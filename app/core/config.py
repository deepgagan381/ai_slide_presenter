"""Central configuration. Everything tunable lives here, loaded from .env with sane defaults."""
import os
from pathlib import Path
from dotenv import load_dotenv

# app/core/config.py -> app/core -> app
APP_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = APP_DIR.parent

load_dotenv(PROJECT_ROOT / ".env")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


# --- Paths (all runtime artefacts live inside app/) ---
STATIC_DIR = APP_DIR / "static"
CACHE_DIR = APP_DIR / ".deck_cache"
LOG_DIR = APP_DIR / "logs"


# --- Ollama (local LLM) ---
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

# Start `ollama serve` ourselves at boot if it is not already running, so uvicorn
# is the only command needed. Set to false to manage the daemon yourself.
AUTO_START_OLLAMA = os.getenv("AUTO_START_OLLAMA", "true").lower() == "true"

GEN_MODEL = os.getenv("GEN_MODEL", "qwen3:8b")
CHAT_MODEL = os.getenv("CHAT_MODEL", "qwen2.5:3b-instruct-q4_K_M")
CHAT_THINK = os.getenv("CHAT_THINK", "false").lower() == "true"
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "5m")


# --- LLM providers ---------------------------------------------------------
# Ollama is the default: free, offline, no key. Both hosted options go through the
# OpenAI SDK, since Gemini publishes an OpenAI-compatible endpoint.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Cheap-but-capable tier, ~$0.01 a session, both good at schema-constrained JSON.
# Overridable because names move - and gemini-2.5-flash shuts down 2026-10-16.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

GEMINI_BASE_URL = os.getenv(
    "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")


def providers() -> dict[str, dict]:
    return {
        "ollama": {
            "label": "Ollama (local)",
            "hosted": False,
            "gen_model": GEN_MODEL,
            "chat_model": CHAT_MODEL,
            "ready": True,          # refined by a live check in /api/health
            "hint": "",
        },
        "openai": {
            "label": "OpenAI",
            "hosted": True,
            "base_url": OPENAI_BASE_URL,
            "gen_model": OPENAI_MODEL,
            "chat_model": OPENAI_MODEL,
            "ready": bool(OPENAI_API_KEY),
            "hint": "" if OPENAI_API_KEY else "Set OPENAI_API_KEY in .env",
        },
        "gemini": {
            "label": "Gemini",
            "hosted": True,
            "base_url": GEMINI_BASE_URL,
            "gen_model": GEMINI_MODEL,
            "chat_model": GEMINI_MODEL,
            "ready": bool(GEMINI_API_KEY),
            "hint": "" if GEMINI_API_KEY else "Set GEMINI_API_KEY in .env",
        },
    }


def api_key_for(provider: str) -> str:
    return {"openai": OPENAI_API_KEY, "gemini": GEMINI_API_KEY}.get(provider, "")


# --- Deck ---
SLIDE_COUNT = int(os.getenv("SLIDE_COUNT", "6"))


# --- Speech ---
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
STT_MODEL = os.getenv("STT_MODEL", "nova-3")
TTS_MODEL = os.getenv("TTS_MODEL", "aura-2-thalia-en")

# One AudioContext in the browser serves both capture and playback, so the mic
# runs at the same rate as TTS output. Avoids a second context and any resampling.
INPUT_SAMPLE_RATE = 24000
OUTPUT_SAMPLE_RATE = 24000