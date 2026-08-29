"""On-disk cache of synthesised speech, stored per deck.

Narration is deterministic: the same sentence, voice and sample rate always give
the same audio, so a second run of a deck costs nothing and needs no network.

Files live under the deck's own folder and are named by a hash of the SENTENCE.
Two reasons: resuming half way through a slide still hits the cache, which a
whole-utterance key would miss; and a regenerated deck produces new filenames
rather than silently replaying old audio for rewritten text.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

from ..core import config

log = logging.getLogger(__name__)


def audio_dir(deck_dir: Path) -> Path:
    return deck_dir / "audio"


def _path(deck_dir: Path, text: str) -> Path:
    raw = f"{text.strip()}|{config.TTS_MODEL}|{config.OUTPUT_SAMPLE_RATE}"
    return audio_dir(deck_dir) / f"{hashlib.sha256(raw.encode()).hexdigest()[:20]}.pcm"


def get(deck_dir: Path, text: str) -> bytes | None:
    try:
        return _path(deck_dir, text).read_bytes()
    except OSError:
        return None


def put(deck_dir: Path, text: str, data: bytes) -> None:
    """Store COMPLETE audio only - never a cancelled stream, which would replay
    later as a sentence cut off mid-word."""
    if not data:
        return
    try:
        audio_dir(deck_dir).mkdir(parents=True, exist_ok=True)
        _path(deck_dir, text).write_bytes(data)
    except OSError as exc:
        log.warning("Could not cache audio: %s", exc)


def clear(deck_dir: Path) -> None:
    shutil.rmtree(audio_dir(deck_dir), ignore_errors=True)


def stats(deck_dir: Path) -> tuple[int, int]:
    d = audio_dir(deck_dir)
    if not d.exists():
        return 0, 0
    files = list(d.glob("*.pcm"))
    return len(files), sum(f.stat().st_size for f in files)
