"""Concept-level deduplication.

Video-level dedup is the videos.video_id primary key. Here we catch the same idea showing
up in different videos ("5 Excel shortcuts" / "5 Excel Shortcuts!!") using plain fuzzy
string matching from the standard library (difflib). Embeddings would catch paraphrases
too, but they need a model; this is the simple first step CLAUDE.md asks for.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

DEFAULT_THRESHOLD = 0.85  # 1.0 = identical; 0.85 tolerates punctuation/word-order noise
# Character similarity alone calls "Python decorators explained" and "Python generators
# explained" the same (only a few letters differ). Also requiring most *words* to be shared
# fixes that: here 2 of 4 distinct words overlap (0.5), below this bar.
MIN_WORD_OVERLAP = 0.6

_PUNCT_RE = re.compile(r"[^\w\s]")


def normalize_concept(text: str) -> str:
    """Lowercase, drop punctuation and emoji, collapse spaces."""
    return " ".join(_PUNCT_RE.sub(" ", text.lower()).split())


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_concept(a), normalize_concept(b)).ratio()


def word_overlap(a: str, b: str) -> float:
    """Share of distinct words in common (Jaccard index)."""
    wa, wb = set(normalize_concept(a).split()), set(normalize_concept(b).split())
    return len(wa & wb) / len(wa | wb) if wa | wb else 0.0


def is_same_concept(a: str, b: str, threshold: float = DEFAULT_THRESHOLD) -> bool:
    return similarity(a, b) >= threshold and word_overlap(a, b) >= MIN_WORD_OVERLAP


def find_duplicate(
    concept: str, previous: list[str], threshold: float = DEFAULT_THRESHOLD
) -> str | None:
    """Return the first earlier concept that is close enough to count as the same, if any."""
    if not normalize_concept(concept):
        return None
    for other in previous:
        if is_same_concept(concept, other, threshold):
            return other
    return None
