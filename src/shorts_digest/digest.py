"""Pick the best accepted items and render the digest as Markdown and CSV.

Ranking: usefulness, then confidence, then items that had a real transcript, then newest.
Selection walks that ranking and skips a topic once it has ``max_per_topic`` items, so one
busy topic (often AI) can't crowd out the rest. The digest stays short on purpose.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from shorts_digest.config import PROJECT_ROOT

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"

TOPIC_LABELS = {
    "ai_llm": "AI / LLM",
    "data_engineering": "Data Engineering",
    "web_dev": "Web Dev",
    "cloud_devops": "Cloud / DevOps",
    "python": "Python",
    "productivity": "Productivity",
}
_CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}

CSV_COLUMNS = [
    "rank",
    "topic",
    "concept",
    "summary",
    "method_or_tool",
    "difficulty",
    "usefulness",
    "confidence",
    "next_step",
    "url",
    "channel",
    "published_at",
    "video_id",
]


@dataclass
class DigestItem:
    video_id: str
    topic: str
    concept: str
    summary: str
    method_or_tool: str | None
    difficulty: str
    usefulness: int
    confidence: str
    next_step: str | None
    url: str
    channel: str
    published_at: str
    text_source: str
    rank: int = 0


@dataclass
class Digest:
    day: date
    items: list[DigestItem]
    shorts_considered: int
    channels_considered: int


def topic_label(topic: str) -> str:
    return TOPIC_LABELS.get(topic, topic.replace("_", " ").title())


def rank_key(item: DigestItem) -> tuple:
    return (
        item.usefulness,
        _CONFIDENCE_RANK.get(item.confidence, 0),
        item.text_source == "transcript",
        item.published_at,
    )


def select(candidates: list[DigestItem], size: int, max_per_topic: int) -> list[DigestItem]:
    """Best-first, with a per-topic cap. Ranks are assigned in reading order (grouped by topic)."""
    picked: list[DigestItem] = []
    per_topic: dict[str, int] = {}
    for item in sorted(candidates, key=rank_key, reverse=True):
        if len(picked) >= size:
            break
        if per_topic.get(item.topic, 0) >= max_per_topic:
            continue
        per_topic[item.topic] = per_topic.get(item.topic, 0) + 1
        picked.append(item)

    # Group by topic for reading; topics ordered by their best item, items by rank within.
    order = {t: i for i, t in enumerate(dict.fromkeys(p.topic for p in picked))}
    picked.sort(key=lambda p: order[p.topic])
    for i, item in enumerate(picked, start=1):
        item.rank = i
    return picked


def render_markdown(digest: Digest) -> str:
    d = digest.day
    lines = [
        f"# Tech Shorts Digest — {d.day} {d:%b %Y}",
        f"{len(digest.items)} picked from {digest.shorts_considered} new Shorts "
        f"across {digest.channels_considered} channels.",
    ]
    if not digest.items:
        lines += ["", "Nothing passed the filters this time. Nothing to watch today."]
        return "\n".join(lines) + "\n"

    current_topic = None
    for item in digest.items:
        if item.topic != current_topic:
            current_topic = item.topic
            lines += ["", f"## {topic_label(item.topic)}"]
        meta = []
        if item.method_or_tool:
            meta.append(f"Tool/method: {item.method_or_tool}")
        meta += [f"Level: {item.difficulty}", f"Score: {item.usefulness}/5"]
        # Two trailing spaces = a Markdown line break, so each field stays on its own line.
        lines.append(f"{item.rank}. **{item.concept}** — {item.summary}  ")
        lines.append(f"   {' · '.join(meta)}  ")
        if item.next_step:
            lines.append(f"   Next step: {item.next_step}  ")
        lines.append(f"   {item.url} — {item.channel}")
    return "\n".join(lines) + "\n"


def write_outputs(
    digest: Digest, output_dir: Path | None = None, overwrite: bool = True
) -> tuple[Path, Path]:
    """Write digest-YYYY-MM-DD.md/.csv. With ``overwrite=False`` (recorded runs) a second
    run on the same day writes digest-YYYY-MM-DD-2.md instead of replacing what was sent."""
    output_dir = output_dir or DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"digest-{digest.day.isoformat()}"
    n = 1
    while not overwrite and (output_dir / f"{stem}.md").exists():
        n += 1
        stem = f"digest-{digest.day.isoformat()}-{n}"
    md_path, csv_path = output_dir / f"{stem}.md", output_dir / f"{stem}.csv"
    md_path.write_text(render_markdown(digest), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for item in digest.items:
            writer.writerow(vars(item))
    return md_path, csv_path
