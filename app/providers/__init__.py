"""LLM and speech providers, plus the factory that picks between them."""
from __future__ import annotations

from ..core import config
from . import audio_cache
from .base import Chunk, LLMProvider
from .deepgram_stt import DeepgramSTT
from .deepgram_tts import DeepgramTTS, duration_of
from .ollama_llm import OllamaLLM, boot_status, ensure_running, stop_ollama, warmup
from .openai_llm import OpenAICompatLLM, to_strict_schema


def make_llm(provider: str | None = None) -> tuple[LLMProvider, str, str]:
    """Returns (client, gen_model, chat_model). Ollama splits the two jobs across a big and a small model; 
    hosted providers are fast enough for one to do both. Falls back to Ollama without a key, so a missing key degrades rather than crashes."""
    name = (provider or config.LLM_PROVIDER or "ollama").lower()
    spec = config.providers().get(name)

    if spec is None or not spec.get("hosted"):
        return OllamaLLM(), config.GEN_MODEL, config.CHAT_MODEL

    key = config.api_key_for(name)
    if not key:
        return OllamaLLM(), config.GEN_MODEL, config.CHAT_MODEL

    client = OpenAICompatLLM(api_key=key, base_url=spec["base_url"], model=spec["gen_model"])
    return client, spec["gen_model"], spec["chat_model"]


def resolve_provider(provider: str | None) -> str:
    """The provider actually in use, after falling back for a missing key."""
    name = (provider or config.LLM_PROVIDER or "ollama").lower()
    spec = config.providers().get(name)
    if spec is None:
        return "ollama"
    if spec.get("hosted") and not config.api_key_for(name):
        return "ollama"
    return name


__all__ = [
    "audio_cache",
    "Chunk", "LLMProvider",
    "OllamaLLM", "warmup", "ensure_running", "stop_ollama", "boot_status",
    "OpenAICompatLLM", "to_strict_schema",
    "make_llm", "resolve_provider",
    "DeepgramTTS", "DeepgramSTT", "duration_of",
]
