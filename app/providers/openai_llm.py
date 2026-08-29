"""Hosted LLMs over the OpenAI wire format - one class covers OpenAI and Gemini,
since Google publishes a compatible endpoint. The awkward part is schema output:
strict mode rejects keywords our deck schema uses, so see `to_strict_schema()`."""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from .base import Chunk

log = logging.getLogger(__name__)

# Keywords OpenAI strict mode rejects. Dropping them is safe here because Deck.from_dict() enforces the slide count and every field itself.
_UNSUPPORTED = ("minItems", "maxItems", "minimum", "maximum", "pattern", "minLength", "maxLength", "format", "default")


def to_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a JSON Schema for OpenAI strict mode: strip unsupported keywords, and
    give every object `additionalProperties: false` with all properties required."""
    if not isinstance(schema, dict):
        return schema

    out = {k: v for k, v in schema.items() if k not in _UNSUPPORTED}

    if out.get("type") == "object":
        props = {k: to_strict_schema(v) for k, v in (out.get("properties") or {}).items()}
        out["properties"] = props
        out["additionalProperties"] = False
        out["required"] = list(props)          # strict mode requires all of them
    elif out.get("type") == "array" and "items" in out:
        out["items"] = to_strict_schema(out["items"])

    return out


class OpenAICompatLLM:
    """Talks to any endpoint speaking the OpenAI chat-completions format."""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.model = model
        self.base_url = base_url
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def complete_json(self, messages: list[dict[str, Any]], schema: dict[str, Any], model: str | None = None, think: bool = False, timeout: float = 300.0) -> dict[str, Any]:
        model = model or self.model

        # Preferred: the provider enforces the schema for us.
        try:
            resp = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                timeout=timeout,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "reply",
                        "schema": to_strict_schema(schema),
                        "strict": True,
                    },
                },
            )
            return json.loads(resp.choices[0].message.content or "{}")
        except Exception as exc:
            log.info("Strict schema refused by %s (%s) - falling back to JSON mode",
                     self.base_url, type(exc).__name__)

        # Fallback: plain JSON mode. The shape is already described in the prompt, and the caller validates and retries, so this is a soft landing.
        resp = await self._client.chat.completions.create(
            model=model,
            messages=[*messages, {
                "role": "system",
                "content": "Reply with a single JSON object matching this schema:\n" + json.dumps(schema),
            }],
            timeout=timeout,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content or "{}")

    async def chat(self, messages: list[dict[str, Any]], model: str | None = None, think: bool | None = None, timeout: float = 120.0) -> AsyncIterator[Chunk]:
        stream = await self._client.chat.completions.create(
            model=model or self.model, messages=messages, stream=True, timeout=timeout,
        )
        async for event in stream:
            if not event.choices:
                continue
            text = event.choices[0].delta.content or ""
            if text:
                yield Chunk(text=text)
            if event.choices[0].finish_reason:
                yield Chunk(done=True)
                return

    async def health(self) -> tuple[bool, list[str]]:
        """Cheapest real proof the key works: ask what models it can see."""
        try:
            page = await self._client.models.list()
            return True, [m.id for m in page.data][:50]
        except Exception as exc:
            log.warning("Provider at %s unreachable: %s", self.base_url, exc)
            return False, []
