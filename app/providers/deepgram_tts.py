"""Deepgram Aura-2 streaming TTS over one persistent WebSocket - the handshake costs
~1.3s, an audible stall if reconnected per utterance. Measured at 24kHz: first byte
~0.46s after Flush, synthesis ~3x faster than realtime."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import AsyncIterator
import websockets

from ..core import config

log = logging.getLogger(__name__)

# linear16 mono: 2 bytes per sample.
BYTES_PER_SECOND = config.OUTPUT_SAMPLE_RATE * 2

_DONE = object()   # sentinel: this utterance is fully synthesised


class DeepgramTTS:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or config.DEEPGRAM_API_KEY
        self.model = model or config.TTS_MODEL
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._reader: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        
        # Gate on the reader: audio still in flight after a barge-in must not leak into the next utterance's queue, 
        # or that one reads a stale end-marker and returns instantly - the agent goes mute for the rest of the session.
        self._accepting = False
        # Set between Clear and its acknowledgement. Measured: Deepgram SUPPRESSES the abandoned utterance's Flushed and sends Cleared instead, 
        # so Cleared is the only reliable signal that the old stream has actually ended.
        self._settling = False
        # True only between sending Flush and receiving its Flushed. Synthesis runs ~3x realtime, so an interrupt usually lands AFTER the stream already
        # finished - and clearing a finished stream is acknowledged by nothing.
        self._in_flight = False

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def url(self) -> str:
        return (
            "wss://api.deepgram.com/v1/speak"
            f"?encoding=linear16&sample_rate={config.OUTPUT_SAMPLE_RATE}&model={self.model}"
        )


    async def start(self) -> bool:
        if not self.available:
            log.warning("No DEEPGRAM_API_KEY - running silent, slides paced by estimate")
            return False
        try:
            self._ws = await websockets.connect(
                self.url,
                additional_headers={"Authorization": f"Token {self.api_key}"},
                max_size=None,
            )
            self._reader = asyncio.create_task(self._read_loop())
            log.info("TTS connected (%s)", self.model)
            return True
        except Exception as exc:
            log.warning("TTS connect failed, running silent: %s", exc)
            self._ws = None
            return False

    async def _read_loop(self) -> None:
        """Fan every server frame into the queue: audio as bytes, end-of-utterance as a sentinel."""
        try:
            async for msg in self._ws:
                if not self._accepting:
                    continue          # abandoned utterance - drop it on the floor
                if isinstance(msg, (bytes, bytearray)):
                    await self._queue.put(bytes(msg))
                    continue
                event = json.loads(msg)
                kind = event.get("type")
                if kind == "Flushed":
                    self._in_flight = False
                    await self._queue.put(_DONE)
                elif kind == "Cleared":
                    # The abandoned stream is now definitively over. Everything
                    # before this belonged to it; everything after is ours.
                    if self._settling:
                        log.info("TTS stream cleared - safe to start the next utterance")
                    self._settling = False
                elif kind in ("Warning", "Error"):
                    log.warning("TTS %s: %s", kind, event)
                    if kind == "Error":
                        await self._queue.put(_DONE)
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("TTS reader died")
        finally:
            if self._accepting:
                await self._queue.put(_DONE)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Speak `text`, yielding PCM16 chunks until the utterance is complete."""
        if not self._ws or not text.strip():
            return

        # Wait for the previous stream to be confirmed dead before opening the gate,
        # so audio still in flight cannot be mistaken for ours. Bounded, so a missing
        # acknowledgement cannot deadlock the session.
        await self._await_settled()

        # Fresh queue per utterance: leftovers from an abandoned one cannot bleed in.
        self._queue = asyncio.Queue()
        queue = self._queue
        self._accepting = True

        try:
            await self._ws.send(json.dumps({"type": "Speak", "text": text}))
            await self._ws.send(json.dumps({"type": "Flush"}))
            self._in_flight = True
        except websockets.ConnectionClosed:
            log.warning("TTS connection closed mid-utterance")
            self._accepting = False
            return

        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    return
                yield item
        finally:
            # Runs on normal completion AND on cancellation (barge-in), so the gate always closes behind us.
            self._accepting = False

    async def clear(self) -> None:
        """Barge-in: stop accepting, tell Deepgram to drop what it has buffered."""
        self._accepting = False
        if not self._ws:
            return
        if not self._in_flight:
            return          # the stream already ended on its own; nothing to clear
        try:
            await self._ws.send(json.dumps({"type": "Clear"}))
            self._settling = True
            self._in_flight = False
        except websockets.ConnectionClosed:
            return

    async def _await_settled(self, timeout: float = 1.0) -> None:
        """Block until Cleared arrives, or give up. Giving up is safe: it only happens when the acknowledgement never comes, 
        in which case nothing more is arriving either."""
        if not self._settling:
            return
        deadline = time.monotonic() + timeout
        while self._settling and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        if self._settling:
            log.warning("No Cleared within %.1fs - starting anyway", timeout)
            self._settling = False

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws:
            try:
                await self._ws.send(json.dumps({"type": "Close"}))
                await self._ws.close()
            except Exception:
                pass
        self._ws = None


def duration_of(num_bytes: int) -> float:
    """Playback seconds for a PCM16 payload - the basis of slide pacing."""
    return num_bytes / BYTES_PER_SECOND
