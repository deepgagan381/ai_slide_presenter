"""FastAPI entrypoint: deck generation over REST, the live session over WebSocket.
The only module directly in app/ - everything else is grouped by domain into
core/, deck/, presentation/ and providers/."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .core import config, setup_logging
from .deck import Deck, DeckError, generate_deck, list_cached, load_cached
from .presentation import PresentationSession, State
from .providers import (DeepgramSTT, DeepgramTTS, OllamaLLM, audio_cache, boot_status, make_llm, resolve_provider, stop_ollama, warmup)

setup_logging()
log = logging.getLogger(__name__)
FALSE_ALARM_TIMEOUT = 1.5
BARGE_IN_MIN_CHARS = 5
BARGE_IN_MIN_CONFIDENCE = 0.5


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("Starting up - static=%s cache=%s logs=%s",config.STATIC_DIR.name, config.CACHE_DIR.name, config.LOG_DIR.name)
    task = asyncio.create_task(warmup())
    yield
    task.cancel()
    await stop_ollama()
    log.info("Shutdown complete")

app = FastAPI(
    title="AI Slide Presenter", 
    lifespan=lifespan
)



def _model_present(name: str, installed: list[str]) -> bool:
    """Is this exact model pulled? Not a family-prefix match, which would report qwen3:8b present when only qwen3:14b is installed. 
    A tag that EXTENDS the configured one counts, so "qwen2.5:3b" accepts "qwen2.5:3b-instruct-q4_K_M"."""
    return name in installed or any(m.startswith(name) for m in installed)

@app.get("/api/health")
async def health(provider: str | None = None):
    active = resolve_provider(provider)
    spec = config.providers()[active]

    # A hosted provider needs no local model check - just a working key.
    if spec["hosted"]:
        return {
            "provider": active, 
            "label": spec["label"], 
            "hosted": True,
            "ollama": True, 
            "state": "ready", 
            "settling": False,
            "model": spec["gen_model"], 
            "required": [], 
            "missing": [],
            "deepgram_key": bool(config.DEEPGRAM_API_KEY),
        }


    ok, installed = await OllamaLLM().health()
    state = "ready" if ok else boot_status()
    # Both matter and fail at different moments: a missing chat model breaks the first question, a missing generation model breaks the first deck.
    required = [
        {
            "role": "generation", 
            "name": config.GEN_MODEL,
            "present": _model_present(config.GEN_MODEL, installed)
        },
        {
            "role": "chat", 
            "name": config.CHAT_MODEL,
            "present": _model_present(config.CHAT_MODEL, installed)
        },
    ]

    return {
        "provider": active,
        "label": spec["label"],
        "hosted": False,
        "ollama": ok,
        "state": state,
        "settling": state in ("starting", "unknown"),
        "installed": installed,
        "required": required,
        "missing": [m["name"] for m in required if not m["present"]],
        "gen_model": config.GEN_MODEL,
        "chat_model": config.CHAT_MODEL,
        "deepgram_key": bool(config.DEEPGRAM_API_KEY),
    }


@app.get("/api/providers")
async def api_providers():
    """What the landing page offers. A hosted provider with no key is listed but not selectable, so the UI can explain why instead of failing on first use."""
    return {
        "default": resolve_provider(None),
        "providers": [
            {   
                "id": pid, 
                "label": spec["label"], 
                "hosted": spec["hosted"],
                "ready": spec["ready"], 
                "hint": spec["hint"],
                "model": spec["gen_model"]
            }
            for pid, spec in config.providers().items()
        ],
    }


@app.get("/api/decks")
async def api_decks():
    """The library: every deck already generated, newest first."""
    return {"decks": list_cached()}


@app.get("/api/deck")
async def api_deck(topic: str):
    """Fetch one cached deck. 404 means it has not been generated yet, which is what flips the landing page's action button from Start to Generate."""
    deck = load_cached(topic)
    if deck is None:
        return JSONResponse({"error": "No deck for that topic yet."}, status_code=404)
    return deck.to_dict()


@app.post("/api/generate")
async def api_generate(payload: dict):
    topic = (payload or {}).get("topic", "").strip()
    if not topic:
        return JSONResponse({"error": "A topic is required."}, status_code=400)
    topic = topic[:200]
    provider = resolve_provider((payload or {}).get("provider"))
    force = bool((payload or {}).get("force"))

    llm, gen_model, chat_model = make_llm(provider)
    try:
        deck = await generate_deck(topic, llm=llm, gen_model=gen_model, chat_model=chat_model, use_cache=not force)
    except DeckError as exc:
        log.warning("Generation rejected for %r: %s", topic, exc)
        return JSONResponse({"error": str(exc)}, status_code=502)
    except Exception as exc:
        log.exception("Generation failed for %r via %s", topic, provider)
        return JSONResponse({"error": f"Generation failed: {exc}"}, status_code=502)
    return deck.to_dict()


@app.websocket("/ws")
async def ws_session(ws: WebSocket):
    await ws.accept()
    session: PresentationSession | None = None
    tts = DeepgramTTS()
    await tts.start()

    unduck: asyncio.Task | None = None

    async def emit(event: dict) -> None:
        try:
            await ws.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass

    async def emit_audio(chunk: bytes) -> None:
        try:
            await ws.send_bytes(chunk)
        except (WebSocketDisconnect, RuntimeError):
            pass

    async def resume_after_false_alarm() -> None:
        """No words followed the VAD trigger, so it was noise. Carry on talking."""
        await asyncio.sleep(FALSE_ALARM_TIMEOUT)
        await emit({"type": "unduck"})

    async def on_speech(ev: dict) -> None:
        """Barge-in in two stages. VAD fires ~0.7s before the first word: acting on it alone is instant but a cough triggers it, 
        waiting for words talks over the listener. So pause on VAD, commit on real words, resume if none arrive."""
        nonlocal unduck
        if session is None:
            return
        kind = ev["kind"]

        if kind == "speech_started":
            if session.state == State.SPEAKING:
                await emit({"type": "duck"})
                if unduck:
                    unduck.cancel()
                unduck = asyncio.create_task(resume_after_false_alarm())
            return

        if kind == "partial":
            await emit({"type": "partial", "text": ev["text"]})
            if session.state != State.SPEAKING:
                return

            text = ev["text"].strip()
            confidence = ev.get("confidence")
            too_short = len(text) < BARGE_IN_MIN_CHARS
            too_unsure = confidence is not None and confidence < BARGE_IN_MIN_CONFIDENCE
            if too_short or too_unsure:
                # Not confident enough to cancel a sentence. Playback stays ducked, so the unduck timer decides - real speech will produce a better partial.
                log.info("Ignored weak partial %r (len=%d, confidence=%s)",
                         text[:40], len(text), confidence)
                return

            if unduck:
                unduck.cancel()
                unduck = None
            log.info("Barge-in on: %r (confidence=%s)", text[:60], confidence)
            await session.interrupt()
            return

        if kind == "final":
            if unduck:
                unduck.cancel()
                unduck = None
            await emit({"type": "partial", "text": ""})
            if session.busy():
                await session.interrupt()
            session.spawn(session.answer(ev["text"]))

    stt = DeepgramSTT(on_speech)

    try:
        while True:
            frame = await ws.receive()
            if frame.get("type") == "websocket.disconnect":
                break

            # Binary from the client is always microphone PCM.
            if frame.get("bytes") is not None:
                await stt.send_audio(frame["bytes"])
                continue

            if frame.get("text") is None:
                continue
            msg = json.loads(frame["text"])
            kind = msg.get("type")

            if kind == "init":
                try:
                    deck = Deck.from_dict(msg["deck"]["topic"], msg["deck"])
                except (KeyError, TypeError, DeckError) as exc:
                    await emit({"type": "error", "message": f"Bad deck: {exc}"})
                    continue
                provider = resolve_provider(msg.get("provider"))
                llm, _gen, chat_model = make_llm(provider)
                session = PresentationSession(deck, emit, llm=llm, tts=tts, emit_audio=emit_audio, chat_model=chat_model)
                log.info("Session started: %r (%d slides), voice=%s, llm=%s/%s", deck.title, len(deck.slides), "on" if tts.available else "off", provider, chat_model)
                await emit({
                    "type": "ready", 
                    "slides": len(deck.slides),
                    "voice": tts.available, 
                    "provider": provider,
                    "model": chat_model
                })
                await session.set_state(State.IDLE)
                continue

            if session is None:
                await emit({"type": "error", "message": "Session not initialised."})
                continue

            if kind == "start":
                await session.interrupt()
                # Continue from wherever the deck actually is. Restarting at slide 1 after the user has navigated somewhere is never what they meant.
                begin = int(msg.get("from") or session.current_slide)
                session.spawn(session.present_all(start=session.deck.clamp(begin)))

            elif kind == "user_text":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                # A question always wins over whatever the agent is saying. Same precedence rule that voice barge-in uses.
                if session.busy():
                    await session.interrupt()
                session.spawn(session.answer(text))

            elif kind == "mic_on":
                ok = await stt.start()
                await emit({"type": "mic", "on": ok, "error": None if ok else "Speech-to-text unavailable."})

            elif kind == "mic_off":
                await stt.close()
                await emit({"type": "mic", "on": False})

            elif kind == "audio_cache":
                session.use_audio_cache = bool(msg.get("on", True))
                files, size = audio_cache.stats(session.deck_dir)
                await emit({
                    "type": "audio_cache", 
                    "on": session.use_audio_cache,
                    "files": files, 
                    "bytes": size, 
                    "announce": True
                })

            elif kind == "stop":
                await session.stop()

            elif kind == "interrupt":
                await session.interrupt()

            elif kind == "goto":
                await session.navigate(int(msg.get("n", 1)))

    except WebSocketDisconnect:
        log.info("Session disconnected")
    finally:
        if unduck:
            unduck.cancel()
        if session and session.busy():
            await session.interrupt()
        await stt.close()
        await tts.close()



# Pages
def _page(name: str) -> FileResponse:
    return FileResponse(config.STATIC_DIR / name, headers={"Cache-Control": "no-cache"})


@app.get("/")
async def page_library():
    return _page("index.html")


@app.get("/present")
async def page_present():
    return _page("present.html")


class NoCacheStatic(StaticFiles):
    """Serve assets with revalidation forced. Without Cache-Control browsers cache JS and CSS heuristically 
    and skip revalidating, so edits silently do nothing. "no-cache" means revalidate - the ETag still makes unchanged files a 304."""
    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response

app.mount("/static", NoCacheStatic(directory=config.STATIC_DIR), name="static")
