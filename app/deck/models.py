"""Deck data model and validation. The per-slide `keywords` matter most: they become
the concept -> slide routing table in the presenter prompt, which is what makes
automatic navigation accurate rather than a guess."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..core import config


class DeckError(ValueError):
    """Raised when a generated deck fails validation."""


@dataclass
class Slide:
    n: int
    title: str
    bullets: list[str]
    speaker_notes: str
    keywords: list[str] = field(default_factory=list)


@dataclass
class Deck:
    topic: str
    title: str
    slides: list[Slide]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, topic: str, data: dict[str, Any]) -> "Deck":
        """Parse and validate model output. Raises DeckError with a message that is fed back to the model on retry, so keep the messages specific."""
        if not isinstance(data, dict):
            raise DeckError("Top level must be a JSON object.")

        raw_slides = data.get("slides")
        if not isinstance(raw_slides, list):
            raise DeckError("Missing 'slides' array.")
        if len(raw_slides) != config.SLIDE_COUNT:
            raise DeckError(f"Expected exactly {config.SLIDE_COUNT} slides, got {len(raw_slides)}.")

        slides: list[Slide] = []
        for i, raw in enumerate(raw_slides, start=1):
            if not isinstance(raw, dict):
                raise DeckError(f"Slide {i} is not an object.")

            title = str(raw.get("title", "")).strip()
            if not title:
                raise DeckError(f"Slide {i} has an empty title.")

            bullets = [str(b).strip() for b in raw.get("bullets", []) if str(b).strip()]
            if not bullets:
                raise DeckError(f"Slide {i} has no bullets.")

            notes = str(raw.get("speaker_notes", "")).strip()
            if len(notes) < 40:
                raise DeckError(f"Slide {i} speaker_notes too short - needs 2-3 full spoken sentences.")

            keywords = [str(k).strip().lower() for k in raw.get("keywords", []) if str(k).strip()]

            # Renumber rather than trusting the model's own numbering.
            slides.append(Slide(n=i, title=title, bullets=bullets, speaker_notes=notes, keywords=keywords))

        title = str(data.get("title", "")).strip() or topic
        return cls(topic=topic, title=title, slides=slides)

    def get(self, n: int) -> Slide:
        """Fetch by 1-based number, clamped. An open-weight model asking for slide 9 of 6 must not crash the session."""
        return self.slides[self.clamp(n) - 1]

    def clamp(self, n: int) -> int:
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 1
        return max(1, min(len(self.slides), n))

    def content_table(self) -> str:
        """The full deck as reference material for the presenting model. Titles and keywords pick a slide but cannot answer from one: asked "how many neurons?"
        with only a title, a model says "a vast number". The facts live in the notes."""
        blocks = []
        for s in self.slides:
            kw = ", ".join(s.keywords) if s.keywords else "-"
            blocks.append(
                f"Slide {s.n}: {s.title}\n"
                f"  covers: {kw}\n"
                f"  points: {'; '.join(s.bullets)}\n"
                f"  detail: {s.speaker_notes}"
            )
        return "\n\n".join(blocks)
