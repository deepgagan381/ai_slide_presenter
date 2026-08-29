# AI Slide Presenter

Give it any topic. It writes a six-slide deck, presents it out loud, and jumps to the
right slide when you ask a question — and you can talk over it at any time.

Runs on a **local model by default** (Ollama, free and offline), or on **OpenAI or
Gemini** if you would rather use a key you already have. Speech is Deepgram either way.

---

## What it does

| | |
|---|---|
| **Type a topic** | The model writes 6 slides — titles, bullets, spoken notes, routing keywords |
| **Press Start** | It narrates the deck aloud, advancing on its own |
| **Ask a question** | It jumps to the slide that answers it, says why, then answers |
| **Talk over it** | It stops within ~0.7s, answers you, then picks the talk back up mid-sentence |

Decks are cached, so re-opening a topic is instant.

---

## Quick start

**Prerequisites**

- Python 3.11+ (3.13 recommended)
- A [Deepgram](https://deepgram.com) API key, for the voice — free tier is plenty
- **Either** [Ollama](https://ollama.com) installed, **or** an OpenAI / Gemini API key

That last line is a real choice, not boilerplate: Ollama costs nothing and works
offline but wants ~7GB of models, while a hosted key gets you running in a minute and
generates decks about ten times faster. You can switch between them on the home page.

**1. Environment**

```bash
git clone https://github.com/deepgagan381/ai_slide_presenter.git
cd ai_slide_presenter

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**2. Configure**

```bash
cp .env.example .env
```

Fill in `DEEPGRAM_API_KEY` — that is the only required value. Then pick a model source:

*Local (default, free, offline):* install [Ollama](https://ollama.com), then pull both models.

```bash
# macOS
brew install ollama
# Linux
curl -fsSL https://ollama.com/install.sh | sh
# Windows — installer at https://ollama.com/download

ollama pull qwen3:8b                    # writes the deck   (~5 GB)
ollama pull qwen2.5:3b-instruct-q4_K_M  # answers questions (~2 GB)
```

Roughly 7 GB and a few minutes on a decent connection. You do **not** need to run
`ollama serve` — the app starts the daemon itself if it is not already running, and stops
it again on exit (only if it was the one that started it). `ollama list` confirms what you
have; the home page also names both models and tells you the exact `ollama pull` command if
either is missing.

*Hosted (faster, no downloads):* set either key in `.env` instead.

```ini
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
```

Whichever keys are present appear as selectable options under **Model** on the home
page; the rest are shown greyed out with the variable you need to set. Roughly $0.01
per session either way, and everything else in `.env.example` is optional.

**3. Run**

```bash
uvicorn app.main:app --port 8000
```

Open <http://127.0.0.1:8000>. You don't need to start Ollama — the app starts it if it
isn't running, and stops it again on exit (only if it started it).

> On macOS, avoid port 5000: AirPlay Receiver holds it on both IPv4 and IPv6, and since
> `localhost` resolves to `::1` first, your browser reaches AirPlay instead of the app —
> with no error that points at the real cause.

> **Use Chrome**, and **headphones** if you want to interrupt by voice. Speaker bleed is
> the most common cause of the agent interrupting itself.

---

## Try it

1. Pick a **Model** on the home page — Ollama unless you set a key
2. Type a topic → **Generate deck** (~50s on Ollama, ~5s hosted; instant once cached)
3. **Start presenting** — it begins talking
4. Allow the microphone when prompted; the chip in the header goes green
5. **Interrupt it mid-sentence** by just speaking. Ask about something on a later slide
6. Watch it jump, answer, then say *"Anyway, let's get back to where we were"* and resume
   from the sentence it was cut off on

No microphone? The **Ask** box drives the identical path.

---

## How it works

```text
┌──────────┐  mic PCM16   ┌────────────────────┐   audio    ┌─────────────────┐
│ Browser  │ ───────────▶ │  FastAPI           │ ─────────▶ │ Deepgram Nova-3 │
│          │              │                    │ ◀───────── │  streaming STT  │
│ Audio-   │              │ PresentationSession│  transcript└─────────────────┘
│ Worklets │              │  ┌──────────────┐  │
│          │ ◀─ TTS audio │  │ IDLE         │  │ on a MISS  ┌─────────────────┐
│ deck UI  │ ◀─ slides ── │  │ LISTENING    │  │ ─────────▶ │ LLM: Ollama,    │
│transcript│ ◀─ events ── │  │ THINKING     │  │ ◀───────── │ OpenAI or Gemini│
└──────────┘              │  │ SPEAKING     │  │            └─────────────────┘
                          │  └──────────────┘  │
                          │  owns slide state  │ on a MISS  ┌─────────────────┐
                          └─────────┬──────────┘ ─────────▶ │ Deepgram Aura-2 │
                                    │            ◀───────── │  streaming TTS  │
                        looked up FIRST, always     audio   └─────────────────┘
                                    ▼
        ┌──────────────────────────────────────────────────────┐
        │  app/.deck_cache/<sha256(topic)>/                     │
        │     deck.json      the slides      — written once     │
        │     audio/*.pcm    the speech      — one per sentence │
        └──────────────────────────────────────────────────────┘
             Regenerate deletes audio/ ; it refills on next play
```

The server owns the current slide; the browser only renders what it is told.

> Call-level request flows, the session state machine, and the reasoning behind every
> tuning constant: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**

**Both caches are checked before anything is called.** A deck is written once per
topic; each sentence of speech is synthesised once and replayed thereafter. A fully
cached deck reaches neither the model nor Deepgram's TTS — it presents offline.

### Where the model is used

Three paths, and the busiest one uses no model at all:

| Job | When | Local | Hosted |
|---|---|---|---|
| Write the deck | Once per topic, behind a spinner | `qwen3:8b` | `gpt-4o-mini` / `gemini-2.5-flash` |
| **Narrate a slide** | Every slide | **none** | **none** |
| Answer + route slides | Every question | `qwen2.5:3b` | same as above |

**Narration costs nothing** because `speaker_notes` were written to be spoken when the deck
was generated. Re-generating them live would be slower *and* worse.

**Local splits the work across two models; hosted does not.** A laptop cannot run one model
fast enough for both jobs, so writing gets the 8B (~19 tok/s) where nobody is waiting, and
answering gets the 3B (~46 tok/s, measured on an M1 Pro) where you are. A hosted model is
quick enough to do both, so there is only one to configure.

Switching provider is a click on the home page — no restart. Because decks are cached per
topic, use **↻ Regenerate** if you want the new model to rewrite one you already have.

---

## Design decisions worth knowing

**Routing is a required JSON field, not a tool call.** The 3B would *say* "Let's go to
slide 3" without emitting the call, so the deck never moved. A required field can't be skipped.

**Barge-in is two-stage.** VAD fires ~0.7s before the first word, so playback pauses on VAD
and commits once real words confirm it — and resumes if they never come.

**The noise gate has a floor and a ceiling.** A low percentile of the room, clamped between an
absolute minimum and a cap below speech level — so quiet noise never counts however silent the
room measures, and calibration can never leave the mic deaf. Committing an interrupt also
needs sustained level, a transcript of real length, and Deepgram's confidence score.

**Slides are paced by audio duration, not synthesis time.** Deepgram runs ~3× realtime, so
`bytes / 48000` is the honest length — and a clean cancellation point for barge-in.

**Resume lands on the interrupted sentence, not after it.** The transcript reveals in step
with the audio, so the position is real rather than estimated.

**Speech is cached per sentence.** Deterministic, so a second run is free and needs no
network. Per sentence, not per slide, so a resumed talk still hits.

**It declines what it shouldn't answer.** On-topic but not on a slide gets an honest answer;
off-topic gets a polite refusal. Classified as a required field, with a word-overlap guard so
it can't refuse fair questions.

**Tense follows what actually happened.** "We covered this on slide 2" versus "We'll be
covering this on slide 3" — which needs a record of what was narrated, not just a number.

---

## Cost

Roughly **$0.21 per 10-minute session** on the default setup, nearly all of it speech:

| | |
|---|---|
| Deepgram Nova-3 STT | $0.0048/min |
| Deepgram Aura-2 TTS | $0.030 per 1k characters |
| LLM on Ollama | **$0** — runs locally |
| LLM on `gpt-4o-mini` | ~$0.008 per session |
| LLM on `gemini-2.5-flash` | ~$0.012 per session |

Deepgram's $200 of free credit covers hundreds of sessions, and the hosted model
adds only a cent or so on top.

---

## Layout

```text
app/
├── main.py            FastAPI: /api/generate, /api/deck, /ws, pages
├── core/              config, logging
├── deck/              models · prompts · generator (topic → 6 slides, cached)
├── presentation/      session state machine, slide authority
├── providers/         ollama · openai (also Gemini) · deepgram_stt · deepgram_tts
└── static/            no build step — plain HTML/CSS/JS + AudioWorklets
```

### Configuration

Everything lives in `.env`, and there are only five settings:

| Variable | Default | |
|---|---|---|
| `DEEPGRAM_API_KEY` | — | **required** for voice |
| `OPENAI_API_KEY` | — | optional — unlocks OpenAI in the Model selector |
| `GEMINI_API_KEY` | — | optional — unlocks Gemini |
| `OPENAI_MODEL` | `gpt-4o-mini` | |
| `GEMINI_MODEL` | `gemini-2.5-flash` | shutting down 2026-10-16 |

Model IDs move, so those last two are overridable — a stale default should never be
the thing that blocks you. Local model names, ports, log level and similar all have
sensible defaults in `app/core/config.py`, and every one of them can be overridden by
an environment variable of the same name if you need to.

Logs go to `app/logs/`. Each generated deck gets a folder under `app/.deck_cache/` holding
its `deck.json` and an `audio/` folder of cached speech, so deleting a deck removes its audio
with it. Both are gitignored.

---

## Scope for improvement

Roughly in the order I would do them.

**Stream the answer into speech.** Nothing is spoken until the whole JSON object exists —
about a second of dead air. The schema already puts `slide_number` before `say`.

**Fully local speech.** TTS is a swap ([Piper](https://github.com/rhasspy/piper), Kokoro).
STT is a rebuild: barge-in leans on Deepgram's VAD, interim results and endpointing, so it
would mean [Silero VAD](https://github.com/snakers4/silero-vad) plus faster-whisper.

**Tests in the repo.** Barge-in, interrupt-and-resume and stop were all tested during
development, but the tests live outside the project.

**Prune the audio cache.** It grows one file per sentence and nothing evicts it.

**Multiple sessions.** Sessions are already per-connection; the Ollama lifecycle and the TTS
socket are process-wide.

**Editable decks.** Generation is one-shot — one wrong slide means regenerating all six.

**Word-level resume.** Lands on the sentence today; the offset within it is already tracked.

---

## Known limitations

- **First generation is slow on Ollama** (~50s) — a local 8B writing 1,500 tokens.
  Cached thereafter, and roughly 3-5s on a hosted provider.
- **Cached audio grows without a limit.** Roughly 5-6 MB per deck, and nothing prunes it;
  deleting a deck's folder is the only cleanup.
- **Echo cancellation is the browser's**, so speakers at volume can still cause the agent to
  interrupt itself. Headphones fix it.
- **Single session per server.** Fine for a demo, not multi-user.

---

## What I learned

- WebSockets, and why plain HTTP wasn't an option here.
- Browser audio: worklets, ring buffers, and why interrupting playback is harder than starting it.
- Streaming speech-to-text and text-to-speech, and how differently they behave from normal APIs.
- Turn-taking — knowing someone started talking is easy, knowing they meant it is not.
- Telling a model to do something isn't the same as making it. A required field is.
- Small models get things wrong the same way twice, so you can catch it in code.

---

## A note on how this was built

I work on backend and AI systems; frontend is not my day-to-day. I used Claude for the
browser side — the page layout and styling, and the in-browser audio code that captures the
microphone and plays the agent's speech.
