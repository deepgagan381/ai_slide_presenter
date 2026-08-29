"""Topic -> validated 6-slide deck. A single point of failure, so it is defended
four deep: constrained decoding, validation, a retry with the error fed back, then
a model fallback - plus a disk cache, so a known topic never regenerates at all."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from pathlib import Path

from ..core import config
from .models import Deck, DeckError
from .prompts import deck_json_schema, generation_prompt
from ..providers import OllamaLLM

log = logging.getLogger(__name__)


def _key(topic: str) -> str:
    return hashlib.sha256(
        f"{topic.strip().lower()}::{config.SLIDE_COUNT}".encode()).hexdigest()[:16]

def deck_dir(topic: str) -> Path:
    return config.CACHE_DIR / _key(topic)

def _cache_path(topic: str) -> Path:
    return deck_dir(topic) / "deck.json"


def load_cached(topic: str) -> Deck | None:
    path = _cache_path(topic)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return Deck.from_dict(data["topic"], data)
    except (json.JSONDecodeError, KeyError, DeckError, OSError):
        return None


def list_cached() -> list[dict]:
    """Every deck on disk, newest first - the library on the landing page. A corrupt or half-written file is skipped rather than breaking the whole listing."""
    if not config.CACHE_DIR.exists():
        return []

    decks: list[dict] = []
    for path in config.CACHE_DIR.glob("*/deck.json"):
        try:
            data = json.loads(path.read_text())
            topic = data.get("topic")
            slides = data.get("slides") or []
            if not topic or not slides:
                continue
            decks.append({
                "topic": topic,
                "title": data.get("title") or topic,
                "slide_count": len(slides),
                "first_slide": slides[0].get("title", ""),
                "updated": path.stat().st_mtime,
            })
        except (json.JSONDecodeError, OSError, AttributeError, TypeError):
            log.warning("Skipping unreadable cache file: %s", path.name)

    return sorted(decks, key=lambda d: d["updated"], reverse=True)


def save_cached(deck: Deck) -> None:
    try:
        deck_dir(deck.topic).mkdir(parents=True, exist_ok=True)
        _cache_path(deck.topic).write_text(json.dumps(deck.to_dict(), indent=2))
    except OSError as exc:
        log.warning("Could not cache deck: %s", exc)


async def generate_deck(topic: str, llm=None, gen_model: str | None = None, chat_model: str | None = None, use_cache: bool = True) -> Deck:
    topic = topic.strip()
    if not topic:
        raise DeckError("Topic is empty.")

    if use_cache and (cached := load_cached(topic)):
        log.info("Deck cache hit for %r", topic)
        return cached

    # Regenerating rewrites every sentence, so the stored audio is now for text that no longer exists. Per-deck folders are what make this possible at all.
    audio = deck_dir(topic) / "audio"
    if audio.exists():
        shutil.rmtree(audio, ignore_errors=True)
        log.info("Cleared stale audio for %r", topic)

    llm = llm or OllamaLLM()
    gen_model = gen_model or config.GEN_MODEL
    chat_model = chat_model or config.CHAT_MODEL
    schema = deck_json_schema()
    messages = [
        {"role": "system", "content": generation_prompt()},
        {"role": "user", "content": f"Create the deck. Topic: {topic}"},
    ]

    # (model, extra correction message) - escalating attempts. Escalating attempts. On a hosted provider the two models are the same, 
    # so this is simply three tries with a corrective message in the middle.
    attempts: list[tuple[str, str | None]] = [
        (gen_model, None),
        (gen_model, "retry"),
        (chat_model, None),
    ]

    last_error: Exception | None = None
    for model, mode in attempts:
        msgs = list(messages)
        if mode == "retry" and last_error:
            # Feed the specific validation failure back so the retry is informed.
            msgs.append({
                "role": "user", 
                "content": f"Your previous attempt was rejected: {last_error}. Return corrected JSON matching the schema exactly."
            })
        try:
            started = time.monotonic()
            raw = await llm.complete_json(msgs, schema=schema, model=model, think=False)
            deck = Deck.from_dict(topic, raw)
            log.info("Generated deck for %r with %s in %.1fs", topic, model, time.monotonic() - started)
            save_cached(deck)
            # Free the generation model before the presentation loop needs RAM.
            if model != chat_model and hasattr(llm, "unload"):
                await llm.unload(model)
            return deck
        
        except (DeckError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
            log.warning("Deck generation failed (%s): %s", model, exc)
            last_error = exc

    raise DeckError(f"Could not generate a valid deck after {len(attempts)} attempts: {last_error}")
