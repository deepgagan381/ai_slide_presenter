"""Local LLM via Ollama, plus lifecycle for the daemon itself. Two call shapes:
`chat()` streams, `complete_json()` makes one schema-constrained call - which is
what both deck generation and question answering use."""
from __future__ import annotations

import asyncio
import json, logging
import shutil
import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from ..core import config
from .base import Chunk
log = logging.getLogger(__name__)


class Boot:
    """Where the local daemon is in its life, so /api/health can tell "not installed" and "starting up" apart from "down" - otherwise the page shows an alarming red
    dot for the few seconds Ollama takes to boot, then silently corrects itself."""

    UNKNOWN = "unknown"     # not looked yet
    STARTING = "starting"   # we spawned it, waiting for it to answer
    READY = "ready"
    DOWN = "down"           # installed, but would not come up
    MISSING = "missing"     # no ollama binary on this machine

_boot = Boot.UNKNOWN
_proc: asyncio.subprocess.Process | None = None


def boot_status() -> str:
    return _boot


async def ensure_running(timeout: float = 45.0) -> bool:
    """Start `ollama serve` if it is not already up, so `uvicorn` is the single
    command that runs everything. Detached, so a stray Ctrl+C cannot half-kill it -
    shutdown goes through stop_ollama(), which skips daemons that were not ours."""
    global _boot, _proc
    llm = OllamaLLM()
    if (await llm.health())[0]:
        # Already up - someone else owns it, so leave `_proc` as None.
        _boot = Boot.READY
        return True

    binary = shutil.which("ollama") or "/opt/homebrew/bin/ollama"
    if not Path(binary).exists():
        _boot = Boot.MISSING
        log.error("Ollama is not installed (looked for %s). Install it, or point OLLAMA_URL at a remote instance.", binary)
        return False

    log.info("Ollama not running - starting it (%s serve)", binary)
    _boot = Boot.STARTING
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        sink = open(config.LOG_DIR / "ollama.log", "ab")
        _proc = await asyncio.create_subprocess_exec(binary, "serve", stdout=sink, stderr=sink, start_new_session=True)
    except Exception as exc:
        _boot = Boot.DOWN
        log.error("Could not start Ollama: %s", exc)
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(1.0)
        if (await llm.health())[0]:
            _boot = Boot.READY
            log.info("Ollama is up (took %.1fs)", timeout - (deadline - time.monotonic()))
            return True

    _boot = Boot.DOWN
    log.error("Ollama did not become reachable within %.0fs - see logs/ollama.log", timeout)
    return False

async def stop_ollama() -> None:
    """Shut down the daemon on exit, but only if we started it. Killing an Ollama the
    user started themselves would take their other work down with our web server.
    `_proc` is None in that case, so this is a no-op."""
    global _proc, _boot
    if _proc is None:
        return

    log.info("Stopping the Ollama we started (pid %s)", _proc.pid)
    try:
        _proc.terminate()
        await asyncio.wait_for(_proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        log.warning("Ollama ignored SIGTERM - killing it")
        try:
            _proc.kill()
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass                       # already gone
    finally:
        _proc = None
        _boot = Boot.UNKNOWN

async def warmup() -> None:
    """Boot Ollama if needed, check the models exist, then pull one into RAM."""
    if config.AUTO_START_OLLAMA and not await ensure_running():
        log.warning("Continuing without Ollama - cached decks still present, but generation and questions will fail.")
        return

    ok, models = await OllamaLLM().health()
    if ok:
        # A missing model fails at the worst possible moment otherwise: mid-demo, on the first question, with a raw 404 from Ollama.
        for label, name in (("chat", config.CHAT_MODEL), ("generation", config.GEN_MODEL)):
            if not any(m == name or m.startswith(name.split(":")[0]) for m in models):
                log.warning("The %s model %r is not pulled. Run:  ollama pull %s",
                            label, name, name)

    try:
        async for _ in OllamaLLM().chat([{"role": "user", "content": "hi"}]):
            break
        log.info("Warmed %s", config.CHAT_MODEL)
    except Exception as exc:
        log.warning("Model warm-up skipped: %s", exc)


class OllamaLLM:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = config.OLLAMA_URL.rstrip("/")

    async def chat(self, messages: list[dict[str, Any]], model: str | None = None, think: bool | None = None, timeout: float = 120.0) -> AsyncIterator[Chunk]:
        """Stream a reply token-block by token-block."""
        payload: dict[str, Any] = {
            "model": model or config.CHAT_MODEL,
            "messages": messages,
            "stream": True,
            "keep_alive": config.KEEP_ALIVE,
            "think": config.CHAT_THINK if think is None else think,
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as resp:
                resp.raise_for_status()

                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if "error" in data:
                        raise RuntimeError(f"Ollama error: {data['error']}")

                    text = (data.get("message") or {}).get("content") or ""
                    if text:
                        yield Chunk(text=text)
                    if data.get("done"):
                        yield Chunk(done=True)
                        return

    async def complete_json(self, messages: list[dict[str, Any]], schema: dict[str, Any], model: str, think: bool = False, timeout: float = 300.0) -> dict[str, Any]:
        """Non-streaming, schema-constrained call used for deck generation."""
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "format": schema,
            "think": think,
            "keep_alive": config.KEEP_ALIVE,
            "options": {"temperature": 0.7},
        }
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()

        if "error" in data:
            raise RuntimeError(f"Ollama error: {data['error']}")

        content = (data.get("message") or {}).get("content", "")
        return json.loads(content)

    async def unload(self, model: str) -> None:
        """Free a model from RAM immediately. On a 16GB machine the 8B generation model must not linger while the 3B presentation loop is running."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(
                    f"{self.base_url}/api/chat", json={"model": model, "messages": [], "keep_alive": 0},
                )
        except Exception:
            pass  # Best effort - never let a memory optimisation break the session.

    async def health(self) -> tuple[bool, list[str]]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/api/tags")
                resp.raise_for_status()
                return True, [m["name"] for m in resp.json().get("models", [])]
        except Exception:
            return False, []
