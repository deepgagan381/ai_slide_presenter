"""Deepgram Nova-3 streaming speech-to-text. Normalises the wire format into three
events: speech_started (VAD, too trigger-happy to drive barge-in alone), partial
(interim text, what actually commits a barge-in) and final (the whole utterance)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable
import websockets

from ..core import config

log = logging.getLogger(__name__)

OnEvent = Callable[[dict[str, Any]], Awaitable[None]]


class DeepgramSTT:
    def __init__(self, on_event: OnEvent, api_key: str | None = None) -> None:
        self.on_event = on_event
        self.api_key = api_key or config.DEEPGRAM_API_KEY
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._reader: asyncio.Task | None = None
        self._segments: list[str] = []

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def url(self) -> str:
        return (
            "wss://api.deepgram.com/v1/listen"
            f"?model={config.STT_MODEL}"
            "&encoding=linear16"
            f"&sample_rate={config.INPUT_SAMPLE_RATE}"
            "&channels=1"
            "&interim_results=true"      # needed for live text + barge-in
            "&vad_events=true"           # SpeechStarted
            "&endpointing=300"           # ms of silence that ends a turn
            "&utterance_end_ms=1000"     # backstop UtteranceEnd if endpointing misses
            "&smart_format=true"
        )


    async def start(self) -> bool:
        if not self.available:
            log.warning("No DEEPGRAM_API_KEY - microphone disabled")
            return False
        try:
            self._ws = await websockets.connect(
                self.url,
                additional_headers={"Authorization": f"Token {self.api_key}"},
                max_size=None,
            )
            self._reader = asyncio.create_task(self._read_loop())
            log.info("STT connected (%s @ %dHz)", config.STT_MODEL, config.INPUT_SAMPLE_RATE)
            return True
        except Exception as exc:
            log.warning("STT connect failed, microphone disabled: %s", exc)
            self._ws = None
            return False

    async def send_audio(self, pcm: bytes) -> None:
        if not self._ws:
            return
        try:
            await self._ws.send(pcm)
        except websockets.ConnectionClosed:
            self._ws = None

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                event = json.loads(raw)
                kind = event.get("type")

                if kind == "SpeechStarted":
                    await self.on_event({"kind": "speech_started"})
                    continue

                if kind == "UtteranceEnd":
                    await self._flush_utterance()
                    continue

                if kind != "Results":
                    continue

                alts = (event.get("channel") or {}).get("alternatives") or [{}]
                text = (alts[0].get("transcript") or "").strip()
                # Noise that Deepgram turns into words comes back with low confidence.
                # Absent on some interim frames, so `None` must mean "do not block".
                confidence = alts[0].get("confidence")
                is_final = bool(event.get("is_final"))
                speech_final = bool(event.get("speech_final"))

                if text:
                    # Interim text is the barge-in trigger: actual words, which a
                    # cough or a door slam will not produce.
                    partial = " ".join([*self._segments, text]).strip()
                    await self.on_event({"kind": "partial", "text": partial,
                                         "confidence": confidence})
                    if is_final:
                        self._segments.append(text)

                if speech_final:
                    await self._flush_utterance()

        except (websockets.ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("STT reader died")

    async def _flush_utterance(self) -> None:
        text = " ".join(self._segments).strip()
        self._segments.clear()
        if text:
            await self.on_event({"kind": "final", "text": text})

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
