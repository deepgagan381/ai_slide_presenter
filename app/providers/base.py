"""Shared types and the LLM provider interface."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol


@dataclass
class Chunk:
    """One streamed step from an LLM: some text, or the end of the reply."""
    text: str = ""
    done: bool = False


class LLMProvider(Protocol):
    """What the app needs from a language model, and nothing more. Two implementations - OllamaLLM locally, 
    OpenAICompatLLM for anything speaking the OpenAI wire format. Callers are written against this and know neither."""

    async def complete_json(self, messages: list[dict[str, Any]], schema: dict[str, Any], model: str, think: bool = False, timeout: float = 300.0) -> dict[str, Any]:
        """One schema-constrained call. Both the deck and every answer use this."""
        ...

    async def chat(self, messages: list[dict[str, Any]], model: str | None = None, think: bool | None = None, timeout: float = 120.0) -> AsyncIterator[Chunk]:
        ...

    async def health(self) -> tuple[bool, list[str]]:
        """(reachable, available model names)."""
        ...
