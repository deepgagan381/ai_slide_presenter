"""Deck domain: the slide data model, the prompts that produce it, and generation."""
from .generator import deck_dir, generate_deck, list_cached, load_cached, save_cached
from .models import Deck, DeckError, Slide
from .prompts import ANSWER_SCHEMA, deck_json_schema, generation_prompt, presenter_prompt

__all__ = [
    "Deck", "Slide", "DeckError",
    "generate_deck", "load_cached", "save_cached", "list_cached",
    "deck_dir",
    "ANSWER_SCHEMA", "deck_json_schema", "generation_prompt", "presenter_prompt",
]
