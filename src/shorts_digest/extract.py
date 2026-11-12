"""Rule-based concept extraction (no LLM).

For each Short we look at its text (transcript, or title + description) and:
  1. find known tools/concepts from config/terms.yaml, plus keyboard shortcuts and commands,
  2. count instructional ("how to", "press", "instead of"), hype, news and promo phrases,
  3. turn those counts into the same ConceptExtraction fields an LLM would return.

Every score comes with human-readable ``reasons`` so you can see *why* an item scored
what it did, and tune config/terms.yaml when it gets something wrong. Summaries are
extractive (an actual sentence from the transcript), so nothing is invented.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from shorts_digest.config import PROJECT_ROOT, ConfigError

EXTRACTOR_VERSION = "rules-v1"  # stored with each row; bump when the rules change
DEFAULT_TERMS_PATH = PROJECT_ROOT / "config" / "terms.yaml"

MAX_CONCEPT_WORDS = 10
MAX_SUMMARY_WORDS = 35
THIN_TEXT_WORDS = 25  # less text than this is "too thin to judge"


class ConceptExtraction(BaseModel):
    """Same shape as CLAUDE.md section 7, so an LLM could be swapped in later."""

    concept: str
    method_or_tool: str | None
    topic: str
    summary: str
    difficulty: Literal["beginner", "intermediate", "advanced"]
    usefulness: int = Field(ge=1, le=5)
    is_promotional: bool
    confidence: Literal["low", "medium", "high"]
    next_step: str | None
    reasons: list[str] = []


# --- vocabulary --------------------------------------------------------------------


@dataclass
class Term:
    name: str
    topic: str
    kind: Literal["tool", "concept", "advanced"]
    pattern: re.Pattern


@dataclass
class Vocabulary:
    terms: list[Term]
    signals: dict[str, list[re.Pattern]] = field(default_factory=dict)


def _word_pattern(phrase: str) -> re.Pattern:
    # Whole-word, case-insensitive. (?<!\w) / (?!\w) instead of \b so terms like
    # "next.js", "ci/cd" or "c++" still match at their edges.
    return re.compile(rf"(?<!\w){re.escape(phrase.strip())}(?!\w)", re.IGNORECASE)


def load_vocabulary(path: Path = DEFAULT_TERMS_PATH) -> Vocabulary:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Could not read {path.name}: {exc}") from exc

    terms: list[Term] = []
    for topic, groups in (data.get("topics") or {}).items():
        for kind_key, kind in (
            ("tools", "tool"),
            ("concepts", "concept"),
            ("advanced", "advanced"),
        ):
            for item in (groups or {}).get(kind_key) or []:
                name, aliases = (
                    (item, []) if isinstance(item, str) else (item["name"], item.get("aliases", []))
                )
                for spelling in [name, *aliases]:
                    terms.append(Term(name, topic, kind, _word_pattern(spelling)))

    signals = {
        key: [_word_pattern(p) for p in phrases]
        for key, phrases in (data.get("signals") or {}).items()
    }
    for key in ("howto", "hype", "news", "promo"):
        signals.setdefault(key, [])
    return Vocabulary(terms, signals)


# --- text helpers ------------------------------------------------------------------

# Keyboard shortcuts: "Ctrl+Shift+P", "Alt + equals", "cmd k", "control plus c".
# Bare "key letter" only counts for ctrl/control/alt/cmd so "shift a lot" doesn't match.
_KEY = r"(?:[a-z0-9]|f\d{1,2}|enter|tab|home|end|equals|space|delete|arrow)"
_MOD = r"(?:ctrl|control|alt|option|cmd|shift)"
_SHORTCUT_RE = re.compile(
    rf"\b{_MOD}(?:\s*(?:\+|plus)\s*{_MOD})*\s*(?:\+|plus)\s*{_KEY}\b|\b(?:ctrl|control|alt|cmd)\s+{_KEY}\b",
    re.IGNORECASE,
)
# Things that look like code or commands: pip install x, git rebase, df.merge(), --force
_CODE_RE = re.compile(
    r"\b(?:pip|npm|git|docker|kubectl|brew|apt|uv|ollama)\s+[a-z][\w-]+|"
    r"\b\w+\.\w+\(\)|\b\w+\(\)|(?<!\w)--[a-z][\w-]+",
    re.IGNORECASE,
)
_HASHTAG_RE = re.compile(r"#\w+")
_NON_TEXT_RE = re.compile(r"[^\w\s.,:;!?'\"()+/&-]")  # emojis and symbols


def clean_title(title: str) -> str:
    text = _NON_TEXT_RE.sub(" ", _HASHTAG_RE.sub(" ", title))
    words = text.split()
    concept = " ".join(words[:MAX_CONCEPT_WORDS]).strip(" -|:")
    return concept or title.strip()


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    if len(parts) <= 1 and len(text.split()) > 40:
        # Auto-captions often have no punctuation: fall back to 25-word windows.
        words = text.split()
        parts = [" ".join(words[i : i + 25]) for i in range(0, len(words), 25)]
    return parts


def _count(patterns: list[re.Pattern], text: str) -> int:
    return sum(len(p.findall(text)) for p in patterns)


def _trim(sentence: str, max_words: int = MAX_SUMMARY_WORDS) -> str:
    words = sentence.split()
    return sentence if len(words) <= max_words else " ".join(words[:max_words]) + "…"


# --- extraction --------------------------------------------------------------------


@dataclass
class VideoText:
    video_id: str
    title: str
    description: str
    transcript: str | None
    text_source: str  # "transcript" or "metadata"
    channel_topic: str


def extract(video: VideoText, vocab: Vocabulary, allowed_topics: list[str]) -> ConceptExtraction:
    reasons: list[str] = []
    # Titles are written by the creator to name the idea, so they count alongside the transcript.
    if video.text_source == "transcript" and video.transcript:
        body = video.transcript
    else:
        body = video.description
    text = f"{video.title}\n{body}"
    word_count = len(body.split())

    # 1. Vocabulary matches (count each term once per Short, but remember frequency).
    hits: Counter[str] = Counter()
    found: dict[str, Term] = {}
    term_topics: dict[str, set[str]] = {}  # "excel" lives in two topics, for example
    for term in vocab.terms:
        n = len(term.pattern.findall(text))
        if n:
            hits[term.name] = max(hits[term.name], n)  # aliases/topics of one term: count once
            found.setdefault(term.name, term)
            term_topics.setdefault(term.name, set()).add(term.topic)
    tools = [name for name, _ in hits.most_common() if found[name].kind == "tool"]
    concepts = [name for name, _ in hits.most_common() if found[name].kind != "tool"]
    shortcuts = sorted({m.group(0).strip() for m in _SHORTCUT_RE.finditer(text)})
    code = sorted({m.group(0).strip() for m in _CODE_RE.finditer(body)})

    # 2. Signals. Promo is judged on the spoken text only: almost every description has
    #    links and plugs, so counting those would flag everything.
    # Each shortcut or command shown is a concrete instruction in itself.
    howto = _count(vocab.signals["howto"], body) + 2 * len(shortcuts) + len(code)
    # Long transcripts pick up "use"/"then" by accident, so also look at the rate.
    howto_rate = 100 * howto / max(word_count, 1)
    # Hype in the title is a stronger tell than a passing "crazy" in the transcript.
    hype = _count(vocab.signals["hype"], body) + 2 * _count(vocab.signals["hype"], video.title)
    news = _count(vocab.signals["news"], text)
    promo_text = body if video.text_source == "transcript" else video.title
    promo = _count(vocab.signals["promo"], promo_text)

    # 3. Topic: where most matched terms live; ties and no-matches go to the channel's topic.
    topic_votes: Counter[str] = Counter()
    for name, n in hits.items():
        for t in term_topics[name]:
            topic_votes[t] += n
    topic_votes[video.channel_topic] += 0.5  # gentle tie-breaker toward the channel's topic
    topic = next(
        (t for t, _ in topic_votes.most_common() if t in allowed_topics), video.channel_topic
    )

    # 4. Usefulness, following the CLAUDE.md rubric:
    #    5 specific reusable technique with detail; 4 concrete tool/method + when to use it;
    #    3 real concept at headline level; 2 vague tips/opinion; 1 nothing learnable.
    concrete = bool(tools or shortcuts or code)
    specific = bool(shortcuts or code)
    score = 1
    if hits or shortcuts or code:
        score = 2
        reasons.append(f"mentions: {', '.join((tools + concepts)[:4] + shortcuts[:2]) or 'code'}")
    if score >= 2 and howto >= 2:
        score = 3
        reasons.append(f"instructional language x{howto}")
    if score >= 3 and concrete and howto >= 4 and howto_rate >= 3:
        score = 4
        reasons.append(f"names a concrete tool/method and shows how ({howto_rate:.0f}/100 words)")
    if score >= 4 and specific and howto >= 6 and howto_rate >= 5:
        score = 5
        reasons.append(f"specific steps: {', '.join((shortcuts + code)[:3])}")
    if hype >= 2:
        score -= 1
        reasons.append(f"hype language x{hype}")
    if news >= 2:
        score -= 1
        reasons.append(f"news/opinion language x{news}")

    # 5. Confidence, and the "too thin to judge" rule.
    if word_count < THIN_TEXT_WORDS:
        confidence = "low"
        if score > 2:
            reasons.append(f"only {word_count} words of text: capped at 2")
        score = min(score, 2)
    elif video.text_source == "transcript" and len(hits) >= 2:
        confidence = "high"
    elif hits:
        confidence = "medium"
    else:
        confidence = "low"
    if video.text_source != "transcript":
        reasons.append("no transcript; judged from title + description")
    score = max(1, min(5, score))

    # 6. Difficulty from what was matched.
    if any(found[n].kind == "advanced" for n in hits):
        difficulty = "advanced"
    elif concepts:
        difficulty = "intermediate"
    else:
        difficulty = "beginner"

    # 7. Summary: the sentence carrying the most matched terms and how-to language.
    summary = _best_sentence(body, vocab, found) or clean_title(video.title)

    method = ", ".join((tools[:2] + shortcuts[:1]) or code[:1]) or None
    focus = tools[0] if tools else (concepts[0] if concepts else None)
    next_step = None
    if focus:
        extra = concepts[0] if concepts and concepts[0] != focus else ""
        next_step = f'Search: "{" ".join(filter(None, [focus, extra]))} tutorial"'

    is_promo = promo >= 2
    if is_promo:
        reasons.append(f"promotional phrases x{promo}")

    return ConceptExtraction(
        concept=clean_title(video.title),
        method_or_tool=method,
        topic=topic,
        summary=summary,
        difficulty=difficulty,
        usefulness=score,
        is_promotional=is_promo,
        confidence=confidence,
        next_step=next_step,
        reasons=reasons,
    )


def _best_sentence(body: str, vocab: Vocabulary, found: dict[str, Term]) -> str | None:
    sentences = split_sentences(body)
    if not sentences:
        return None
    matched = [t for t in vocab.terms if t.name in found]

    def score(s: str) -> float:
        terms = sum(1 for t in matched if t.pattern.search(s))
        howto = _count(vocab.signals["howto"], s)
        hype = _count(vocab.signals["hype"], s)
        too_short = len(s.split()) < 6
        return terms * 2 + howto - hype * 2 - (3 if too_short else 0)

    # Earlier sentences win ties: Shorts usually state their point up front.
    best = max(enumerate(sentences), key=lambda p: (score(p[1]), -p[0]))[1]
    return _trim(best)
