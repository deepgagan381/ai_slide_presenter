"""The presentation session: state machine, history, slide authority. Narration and
Q&A are separate paths - narration replays the pre-written notes with no model at
all. Everything spoken goes through `_say()`, the single point barge-in cancels."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from enum import Enum
from typing import Any, Awaitable, Callable

from ..core import config
from ..deck.generator import deck_dir
from ..deck.models import Deck
from ..deck.prompts import ANSWER_SCHEMA, presenter_prompt
from ..providers import OllamaLLM, audio_cache
from ..providers.deepgram_tts import DeepgramTTS, duration_of

log = logging.getLogger(__name__)

Emit = Callable[[dict[str, Any]], Awaitable[None]]
EmitAudio = Callable[[bytes], Awaitable[None]]

# Spoken-word pace used only when TTS is unavailable, so the deck stilladvances at a human rhythm instead of instantly.
WORDS_PER_MINUTE = 150

# Measured against aura-2-thalia-en: 99 characters produced 6.16s of speech. Used to reveal the transcript in step with the audio, 
# which is also what gives us an exact resume position when the listener interrupts mid-sentence.
CHARS_PER_SECOND = 16.0


# Static, so resuming costs no LLM call and no thinking latency - it comes outthe instant the answer finishes.
RESUME_LINE = "Anyway, let's get back to where we were."

# Spoken before an off-deck answer. The model cannot be relied on to hedge - it will state a wrong fact with full confidence - so the attribution is added by the app, where it cannot be forgotten.
OUT_OF_DECK_LEAD = "The slides don't cover this."

# For questions with nothing to do with the subject. The agent is a presenter, not ageneral chatbot, so the model's answer is discarded rather than spoken.
OFF_TOPIC_LINE = "That's outside what I'm presenting today."

# Spoken when the listener skips slides mid-talk. Static, like RESUME_LINE, so thehand-over is instant rather than waiting on the model.
NAV_FORWARD = "Moving ahead to slide {n}."
NAV_BACK = "Let's go back to slide {n}."

# Spoken when a question pulls us off the current slide. Which one depends on whether that slide has actually been presented yet - answering from an unseen slide as though it were already covered is the thing that reads wrong.
UPCOMING_LEAD = "We'll be covering this on slide {n}."
COVERED_LEAD = "We covered this on slide {n}."


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# Cached audio is replayed in frames this size, matching streamed chunk sizes.
_AUDIO_CHUNK = 16384

# Words common enough to appear in any deck; matching on them would make the
# subject check fire on everything.
_STOPWORDS = {
    "what", "this", "that", "with", "from", "your", "about", "does","have", "will", "they", "their", 
    "them", "when", "which", "there", "into", "more", "than", "also", "some", "such", "over", "were"
}


# --- helpers -----------------------------------------------------------------

def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_END.split(text.strip()) if p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _estimate_speech_seconds(text: str) -> float:
    words = max(1, len(text.split()))
    return words / WORDS_PER_MINUTE * 60


def _slide_payload(deck: Deck, n: int) -> dict[str, Any]:
    s = deck.get(n)
    return {"n": s.n, "title": s.title, "bullets": s.bullets, "total": len(deck.slides)}


class State(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"


class PresentationSession:
    def __init__(self, deck: Deck, emit: Emit, llm=None, tts: DeepgramTTS | None = None, emit_audio: EmitAudio | None = None, chat_model: str | None = None) -> None:
        self.deck = deck
        self.emit = emit
        self.emit_audio = emit_audio          # binary frames -> browser
        self.tts = tts
        self.llm = llm or OllamaLLM()
        self.chat_model = chat_model or config.CHAT_MODEL

        # Off replays nothing from disk, so the two paths can be compared by ear.
        self.use_audio_cache = True
        self.deck_dir = deck_dir(deck.topic)

        self.current_slide = 1          # authoritative - the client never decides this
        self.state = State.IDLE
        self.narrated: set[int] = set()  # compact stand-in for narration transcript
        self.history: list[dict[str, Any]] = []
        self._task: asyncio.Task | None = None
        self._spoken_this_turn = ""

        # Where narration got to, so a question can be answered and the talk then
        # picked up from the exact sentence that was cut off.
        self._presenting = False
        self._presenting_slide = 1
        self._sentence_index = 0
        self._resume_point: dict[str, int] | None = None

    # --- lifecycle -----------------------------------------------------------

    async def set_state(self, state: State) -> None:
        self.state = state
        await self.emit({"type": "state", "value": state.value})

    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    async def interrupt(self) -> None:
        """Cancel whatever the agent is doing. This is the barge-in entry point; the STT speech-started event calls straight into here."""
        # Capture the position BEFORE cancelling - the task is about to be torn down.
        was_presenting = self._presenting
        slide, sentence = self._presenting_slide, self._sentence_index
        was_busy = self.busy()

        if self.busy():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        # Drop buffered speech at both ends: Deepgram's queue and the browser's playback ring. 
        # Without the client-side flush the agent keeps talking for seconds after being cut off - all its audio is already downloaded.
        if self.tts:
            await self.tts.clear()
        await self.emit({"type": "flush_audio"})

        if was_presenting and was_busy:
            # Resume from the sentence that was playing, not the one after it. It was only partly heard, so replaying it restores the thread.
            self._resume_point = {"slide": slide, "sentence": max(0, sentence)}
            await self.emit({"type": "resume_available", **self._resume_point})

        self._presenting = False
        # Record only what was actually spoken, so the model never believes it said something the listener did not hear.
        if self._spoken_this_turn.strip():
            self.history.append({"role": "assistant", "content": self._spoken_this_turn.strip()})
            self._spoken_this_turn = ""

        # Only announce an interruption if there was actually something to cut off - otherwise pressing Start prints a phantom "interrupted" in the transcript.
        if was_busy:
            await self.emit({"type": "interrupted"})
        await self.set_state(State.LISTENING)

    async def stop(self) -> None:
        """Full stop, unlike interrupt(). interrupt() is a pause with intent to return, so it keeps a resume point. 
        stop() means the listener is done: forget where we were, so nothing starts speaking again on its own."""
        await self.interrupt()
        self._resume_point = None
        self._presenting = False
        self.narrated.clear()
        await self.emit({"type": "stopped"})
        await self.set_state(State.IDLE)

    def spawn(self, coro) -> None:
        self._task = asyncio.create_task(self._guard(coro))

    async def _guard(self, coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Session task failed")
            await self.emit({"type": "error", "message": str(exc)})
            await self.set_state(State.IDLE)

    # --- speaking ------------------------------------------------------------

    async def _say(self, text: str, base_index: int = 0) -> None:
        """Single seam for transcript, audio and pacing. Deepgram synthesises ~3x faster than realtime, 
        so returning when the bytes are sent rips through the deck in seconds. We sleep out the real PLAYBACK duration instead."""
        text = text.strip()
        if not text:
            return

        sentences = _split_sentences(text)
        await self.set_state(State.SPEAKING)
        started = time.monotonic()

        # Audio streams in the background so the transcript reveals in step with it. That sync is what makes `_sentence_index` a truthful resume position.
        audio_task: asyncio.Task | None = None
        if self.tts and self.tts.available and self.emit_audio:
            await self.emit({"type": "audio_start"})
            audio_task = asyncio.create_task(self._stream_audio(sentences))

        try:
            for i, sentence in enumerate(sentences):
                self._sentence_index = base_index + i
                self._spoken_this_turn += sentence + " "
                await self.emit({"type": "assistant_delta", "text": sentence + " "})

                # Cancellation landing inside this sleep tells us exactly which sentence was playing when the listener interrupted.
                await asyncio.sleep(len(sentence) / CHARS_PER_SECOND)

            if audio_task is not None:
                total = await audio_task
                await self.emit({"type": "audio_end"})
                speech_seconds = duration_of(total)
            else:
                speech_seconds = _estimate_speech_seconds(text)

            # Character-rate is an estimate; settle up against the real duration.
            remaining = speech_seconds - (time.monotonic() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)

        except asyncio.CancelledError:
            if audio_task is not None and not audio_task.done():
                audio_task.cancel()
            raise

    async def _stream_audio(self, sentences: list[str]) -> int:
        """Push PCM to the browser sentence by sentence; returns bytes for pacing.

        Per sentence rather than per utterance so the cache still hits when a talk resumes half way through a slide. 
        Nothing is written for a cancelled stream, which would replay later as a sentence cut off mid-word.
        """
        total = 0
        stored = False
        for sentence in sentences:
            cached = audio_cache.get(self.deck_dir, sentence) if self.use_audio_cache else None
            if cached:
                # One 500KB frame would arrive as a lump; chunk it so the browser ring buffer fills the way it does when streaming.
                for i in range(0, len(cached), _AUDIO_CHUNK):
                    await self.emit_audio(cached[i:i + _AUDIO_CHUNK])
                total += len(cached)
                continue

            buffer = bytearray()
            async for chunk in self.tts.synthesize(sentence):
                await self.emit_audio(chunk)
                buffer.extend(chunk)

            # Reached only on a complete stream - a cancel raises out of the loop.
            audio_cache.put(self.deck_dir, sentence, bytes(buffer))
            stored = True
            total += len(buffer)

        if stored:
            # Keeps the on-screen count honest as a deck warms up, without a message per sentence.
            files, size = audio_cache.stats(self.deck_dir)
            await self.emit({
                "type": "audio_cache", 
                "on": self.use_audio_cache,
                "files": files, 
                "bytes": size, 
                "announce": False
            })
        return total

    async def _finish_turn(self, record: bool = True) -> None:
        spoken = self._spoken_this_turn.strip()
        self._spoken_this_turn = ""
        if spoken and record:
            self.history.append({"role": "assistant", "content": spoken})
        await self.emit({"type": "assistant_done", "text": spoken})

    # --- slide control -------------------------------------------------------

    async def goto(self, n: int, reason: str = "") -> int:
        n = self.deck.clamp(n)
        changed = n != self.current_slide
        self.current_slide = n
        if changed:
            await self.emit({
                "type": "slide_change",
                "n": n,
                "reason": reason,
                "slide": _slide_payload(self.deck, n),
            })
        return n

    # --- presenting ----------------------------------------------------------

    async def _narrate(self, n: int, from_sentence: int = 0) -> None:
        """Speak a slide's prepared notes. Deliberately NOT added to `history`: six slides of narration in context measurably 
        degrades slide routing, since the model then believes it has already covered everything."""
        notes = _split_sentences(self.deck.get(n).speaker_notes)[from_sentence:]
        if not notes:
            self.narrated.add(n)
            return

        self._presenting = True
        self._presenting_slide = n
        await self._say(" ".join(notes), base_index=from_sentence)
        self.narrated.add(n)
        await self._finish_turn(record=False)

    async def navigate(self, n: int) -> None:
        """Manual slide change from the dots or prev/next. If a talk is underway it carries on from the new slide, 
        the way a presenter would. When nothing is being presented this is just browsing, so it stays quiet."""
        target = self.deck.clamp(n)
        if target == self.current_slide:
            return

        was_presenting = self._presenting or self.busy()
        forward = target > self.current_slide

        await self.interrupt()
        await self.goto(target, "manual navigation")

        if not was_presenting:
            return

        # The listener chose somewhere new, so the old resume point is void - otherwise the next answer would hand back to a slide they left behind.
        self._resume_point = None
        lead = (NAV_FORWARD if forward else NAV_BACK).format(n=target)
        self.spawn(self._present_from(target, lead))

    async def _present_from(self, n: int, lead: str) -> None:
        """Announce the jump, then carry on through the rest of the deck."""
        self._presenting = True
        self._presenting_slide = n
        self._sentence_index = 0
        await self._say(lead)
        await self._finish_turn(record=False)
        await self.present_all(start=n, reason="")

    async def present_all(self, start: int = 1, from_sentence: int = 0, reason: str = "starting the presentation") -> None:
        """Walk the deck from `start`, cancelled cleanly by interrupt(). `from_sentence` applies only to the first slide - it is how a resumed talk picks up mid-slide instead of restarting it."""
        await self.goto(start, reason)
        for n in range(start, len(self.deck.slides) + 1):
            if n != self.current_slide:
                await self.goto(n)
            await self._narrate(n, from_sentence if n == start else 0)
            await asyncio.sleep(0.4)  # breath between slides

        self._presenting = False
        self._resume_point = None
        await self.emit({"type": "presentation_complete"})
        await self.set_state(State.LISTENING)

    # --- question answering --------------------------------------------------

    async def answer(self, question: str) -> None:
        """Route and answer in ONE schema-constrained call. Not tool calling: a 3B degrades mid-conversation into *narrating* 
        the intent ("Let's go to slide 3") without emitting the call. A required field cannot be skipped."""

        await self.emit({"type": "transcript", "role": "user", "text": question})
        self.history.append({"role": "user", "content": question})
        # Answering is not presenting: an interrupt here must not overwrite the resume point we are holding for the talk.
        self._presenting = False
        await self.set_state(State.THINKING)

        # Only the real user exchange, kept short so the live question stays the most salient thing in context.
        messages = [
            {"role": "system", "content": presenter_prompt(self.deck, self.current_slide, sorted(self.narrated))},
            *self.history[-6:],
        ]

        result = await self.llm.complete_json(messages, schema=ANSWER_SCHEMA, model=self.chat_model, think=config.CHAT_THINK)

        previous = self.current_slide
        target = self.deck.clamp(result.get("slide_number", self.current_slide))
        reason = str(result.get("reason", "")).strip()
        say = str(result.get("say", "")).strip()

        # A required enum beats asking the model to hedge in prose - it confidently denied Deepgram had text-to-speech when the deck merely omitted it. The override below is the second guard: it also over-declines fair questions.
        relevance = str(result.get("relevance", "in_deck"))
        if relevance == "unrelated" and self._mentions_subject(question):
            log.info("Overriding 'unrelated' - question uses the deck's vocabulary")
            relevance = "related"

        if relevance == "unrelated":
            say = OFF_TOPIC_LINE
        elif say and relevance == "related":
            say = f"{OUT_OF_DECK_LEAD} From what I know, {say}"
        elif say and target != previous and self.narrated:
            # `narrated` is empty until Start is pressed, and telling someone who never began the talk that we will "cover this later" would be absurd.
            lead = COVERED_LEAD if target in self.narrated else UPCOMING_LEAD
            say = f"{lead.format(n=target)} {say}"

        # An unrelated question is not a reason to move the deck.
        if target != previous and relevance != "unrelated":
            await self.goto(target, reason or "answering your question")

        if say:
            await self._say(say)
        await self._finish_turn()

        # Question answered - pick the talk back up where it was cut off.
        if self._resume_point:
            await self._resume()
        else:
            await self.set_state(State.LISTENING)

    def _mentions_subject(self, question: str) -> bool:
        """Does the question use any word from the deck's own vocabulary?

        The model over-declines: asked "can brain cells regenerate?" against a deck on the brain it answered "unrelated". 
        A cheap word overlap check cannot be arguedwith, and refusing a fair question is worse than answering a stray one."""
        text = question.lower()
        terms: set[str] = set()
        for phrase in [
            self.deck.topic, 
            self.deck.title, 
            *(s.title for s in self.deck.slides),
            *(k for s in self.deck.slides for k in s.keywords)
        ]: terms |= set(re.findall(r"[a-z]{4,}", phrase.lower()))
        return any(t in text for t in terms - _STOPWORDS)

    async def _resume(self) -> None:
        """Return to the interrupted point and carry on. The bridging line is a fixed string, not model output: no round trip, 
        so it starts the moment the answer ends instead of adding a second pause for thought."""
        point = self._resume_point
        self._resume_point = None

        # Re-arm before speaking, so interrupting the bridge line itself simply re-records the same position rather than losing it.
        self._presenting = True
        self._presenting_slide = point["slide"]
        self._sentence_index = point["sentence"]

        await self.emit({"type": "resuming", **point})
        await self.goto(point["slide"], "back to where we were")
        await self._say(RESUME_LINE, base_index=point["sentence"])
        await self._finish_turn(record=False)
        await self.present_all(
            start=point["slide"],
            from_sentence=point["sentence"],
            reason="back to where we were"
        )

