"""Discovery: find small, rising creators whose Shorts do unusually well for their size.

Steps (CLAUDE.md section 6a):

1. **Search** - one ``search.list`` per keyword chosen for today (100 units each, max 15 a
   day). Keywords rotate round-robin across days; ``order`` alternates viewCount / date.
2. **Enrich** - ``videos.list`` (duration, views) and ``channels.list`` (subscribers,
   uploads) in batches of 50. Channel stats are cached for a week.
3. **Filter** - every candidate gets a drop reason or is kept. Nothing is discarded
   silently: dropped candidates are stored with their reason too.
4. **Rank and cap** - ``outlier_score = views / subscribers``; at most one video per channel
   per day, and at most ``max_candidates_per_day`` kept.

A dry run does all of this with real API calls (so it spends real quota) but stores nothing
and does not advance the keyword rotation.

Phase 8 stops here. Phase 9 sends kept candidates through text + extraction.
"""

from __future__ import annotations

import csv
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from shorts_digest import store
from shorts_digest.config import AppConfig, DiscoveryConfig
from shorts_digest.dedupe import normalize_concept, similarity, word_overlap
from shorts_digest.extract import Vocabulary, load_vocabulary
from shorts_digest.quota import COSTS, PACIFIC, QuotaGuard, QuotaLimitError
from shorts_digest.shorts import is_short_by_duration, parse_duration
from shorts_digest.youtube import channels_by_id, fetch_video_details, search_short_videos

log = logging.getLogger(__name__)

ORDERS = ("viewCount", "date")
POSITION_KEY = "keyword_position"
REPOST_WINDOW_DAYS = 30
REPOST_MIN_WORDS = 3  # "python tip" from two channels is a coincidence, not a repost
REPOST_SIMILARITY = 0.85
REPOST_WORD_OVERLAP = 0.6

# Drop reasons, in the order they are checked (the first that applies is recorded).
DROP_REASONS = (
    "video_unavailable",  # deleted/private between search and lookup
    "not_short",  # longer than max_short_seconds
    "curated_channel",  # already in channels.yaml
    "channel_rejected",
    "channel_blocked",
    "channel_unavailable",
    "wrong_language",  # not in discovery.languages
    "off_topic",  # title + description mention no tech term from terms.yaml
    "subscribers_hidden",
    "too_few_subscribers",
    "too_many_subscribers",
    "too_few_uploads",
    "too_few_views",
    "low_views_per_subscriber",
    "likely_repost",  # same title as an earlier video from another channel
    "channel_daily_cap",  # one video per channel per day
    "over_daily_cap",  # max_candidates_per_day reached
)


@dataclass
class Candidate:
    video_id: str
    channel_id: str
    title: str
    published_at: str
    keyword: str
    search_order: str
    duration_seconds: int | None = None
    views: int | None = None
    channel_title: str = ""
    subscribers: int | None = None
    video_count: int | None = None
    outlier_score: float | None = None
    drop_reason: str | None = None
    description: str = ""
    language: str | None = None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/shorts/{self.video_id}"

    @property
    def kept(self) -> bool:
        return self.drop_reason is None


@dataclass
class DiscoveryResult:
    keywords: list[str] = field(default_factory=list)
    searches: int = 0
    hits: int = 0  # unique videos returned by all searches
    already_seen: int = 0  # judged on an earlier day (or a curated video)
    candidates: list[Candidate] = field(default_factory=list)
    stopped: str | None = None  # why searching ended early, if it did
    dry_run: bool = False

    @property
    def kept(self) -> list[Candidate]:
        return sorted((c for c in self.candidates if c.kept), key=lambda c: -(c.outlier_score or 0))

    def drop_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self.candidates:
            if c.drop_reason:
                counts[c.drop_reason] = counts.get(c.drop_reason, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: DROP_REASONS.index(kv[0])))


# --- small pure helpers (easy to test) ---------------------------------------------


def todays_keywords(keywords: list[str], position: int, n: int) -> list[str]:
    """The next ``n`` keywords starting at ``position``, wrapping; never repeats one."""
    if not keywords:
        return []
    n = min(n, len(keywords))
    return [keywords[(position + i) % len(keywords)] for i in range(n)]


def order_for(index: int, day: str) -> str:
    """Alternate viewCount/date across keywords, and flip daily so each keyword gets both."""
    return ORDERS[(index + date.fromisoformat(day).toordinal()) % 2]


def outlier_score(views: int | None, subscribers: int | None) -> float | None:
    if views is None or subscribers is None:
        return None
    return views / max(subscribers, 1)


# Hashtags glue words together (#vibecoding, #codinghumour), so whole-word matching misses
# them. These stems are specific enough to match *inside* a hashtag; short words like "ai",
# "dev" or "app" are not (they hide in #daily, #devotional, #happy).
HASHTAG_STEMS = (
    "coding",
    "coder",
    "programm",
    "developer",
    "software",
    "python",
    "javascript",
    "excel",
    "linux",
    "sql",
    "chatgpt",
    "github",
)
_HASHTAG_RE = re.compile(r"#(\w+)")


def on_topic(text: str, vocab: Vocabulary) -> bool:
    if vocab.mentions_tech(text):
        return True
    return any(stem in tag.lower() for tag in _HASHTAG_RE.findall(text) for stem in HASHTAG_STEMS)


# Script each allowed language is written in, for videos without a language tag.
# Hinglish is Hindi in Latin script, so it passes as Latin.
LANGUAGE_SCRIPTS = {"en": "LATIN", "hi": "DEVANAGARI"}


def language_ok(language: str | None, title: str, allowed: list[str]) -> bool:
    """YouTube's language tag decides when present; otherwise every letter in the title
    must be in a script of an allowed language. "zxx" = no speech (e.g. music): allowed."""
    if language:
        primary = language.split("-")[0].lower()
        return primary in allowed or primary == "zxx"
    scripts = {LANGUAGE_SCRIPTS[lang] for lang in allowed if lang in LANGUAGE_SCRIPTS}
    if not scripts:
        return True
    for ch in title:
        if ch.isalpha() and unicodedata.name(ch, "").split(" ")[0] not in scripts:
            return False
    return True


def check_candidate(
    c: Candidate,
    d: DiscoveryConfig,
    max_short_seconds: int,
    curated_ids: set[str],
    channel_status: dict[str, str],
    known_channels: set[str],
    vocab: Vocabulary | None = None,
) -> str | None:
    """First drop reason that applies (before repost and cap checks), or None to keep."""
    if c.duration_seconds is None:
        return "video_unavailable"
    if not is_short_by_duration(c.duration_seconds, max_short_seconds):
        return "not_short"
    if c.channel_id in curated_ids:
        return "curated_channel"
    status = channel_status.get(c.channel_id)
    if status in ("rejected", "blocked"):
        return f"channel_{status}"
    if c.channel_id not in known_channels:
        return "channel_unavailable"
    if not language_ok(c.language, c.title, d.languages):
        return "wrong_language"
    if vocab is not None and not on_topic(f"{c.title}\n{c.description}", vocab):
        return "off_topic"
    if c.subscribers is None:
        return "subscribers_hidden"
    if c.subscribers < d.min_subscribers:
        return "too_few_subscribers"
    if c.subscribers > d.max_subscribers:
        return "too_many_subscribers"
    if (c.video_count or 0) < d.min_uploads:
        return "too_few_uploads"
    if c.views is None or c.views < d.min_views:
        return "too_few_views"
    if (c.outlier_score or 0) < d.min_views_per_subscriber:
        return "low_views_per_subscriber"
    return None


def same_title(a: str, b: str) -> bool:
    """Near-identical titles. Word overlap first: it's cheap and rules most pairs out."""
    if len(normalize_concept(a).split()) < REPOST_MIN_WORDS:
        return False
    return word_overlap(a, b) >= REPOST_WORD_OVERLAP and similarity(a, b) >= REPOST_SIMILARITY


def mark_reposts(candidates: list[Candidate], stored: list[tuple[str, str, str]]) -> None:
    """Drop a surviving candidate whose title matches an *earlier* video from another channel.

    ``stored`` = (title, channel_id, published_at) of videos we already have. Every candidate
    in this batch also counts as a reference (even dropped ones: a big channel's original can
    be what a small channel re-uploaded), so the earliest copy is the one that survives.
    """
    refs = list(stored)
    for c in sorted(candidates, key=lambda c: c.published_at):
        if c.kept and any(
            ch != c.channel_id and pub <= c.published_at and same_title(c.title, title)
            for title, ch, pub in refs
        ):
            c.drop_reason = "likely_repost"
        refs.append((c.title, c.channel_id, c.published_at))


def rank_and_cap(
    candidates: list[Candidate], channels_kept_today: set[str], kept_today: int, cap: int
) -> None:
    """Best outlier first; one per channel per day; at most ``cap`` kept per day."""
    taken = set(channels_kept_today)
    kept = kept_today
    for c in sorted(
        (c for c in candidates if c.kept), key=lambda c: c.outlier_score or 0, reverse=True
    ):
        if c.channel_id in taken:
            c.drop_reason = "channel_daily_cap"
        elif kept >= cap:
            c.drop_reason = "over_daily_cap"
        else:
            taken.add(c.channel_id)
            kept += 1


def _day_start_utc(day: str) -> str:
    start = datetime.combine(date.fromisoformat(day), time(0), tzinfo=PACIFIC)
    return start.astimezone(UTC).isoformat(timespec="seconds")


# --- the job -----------------------------------------------------------------------


def discover(
    cfg: AppConfig,
    conn: sqlite3.Connection,
    client: Any,
    guard: QuotaGuard,
    *,
    dry_run: bool = False,
    max_searches: int | None = None,
    now: datetime | None = None,
    vocab: Vocabulary | None = None,
) -> DiscoveryResult:
    d = cfg.settings.discovery
    vocab = vocab or load_vocabulary()
    now = now or datetime.now(UTC)
    result = DiscoveryResult(dry_run=dry_run)
    day = guard.day

    # 1. Search ---------------------------------------------------------------------
    allowed = d.max_searches_per_day - store.searches_on(conn, day)
    if max_searches is not None:
        allowed = min(allowed, max_searches)
    if allowed <= 0:
        result.stopped = f"daily search cap reached ({d.max_searches_per_day} on {day})"
        return result
    position = int(store.get_state(conn, POSITION_KEY, "0")) % max(len(d.keywords), 1)
    result.keywords = todays_keywords(d.keywords, position, allowed)
    published_after = now - timedelta(days=d.lookback_days)

    hits: dict[str, Candidate] = {}
    for i, keyword in enumerate(result.keywords):
        order = order_for(i, day)
        try:
            found = search_short_videos(
                client,
                keyword,
                order,
                published_after,
                d.relevance_language,
                d.safe_search,
                guard,
            )
        except QuotaLimitError as exc:  # stop searching, but judge what we already have
            result.stopped = str(exc)
            log.warning("Discovery stopped searching: %s", exc)
            break
        result.searches += 1
        store.log_search(conn, day, keyword, order, len(found), COSTS["search.list"], dry_run)
        log.info(
            "Search %r order=%s: %d results (%d units)",
            keyword,
            order,
            len(found),
            COSTS["search.list"],
        )
        for h in found:  # the first keyword that found a video gets the credit
            hits.setdefault(
                h.video_id,
                Candidate(h.video_id, h.channel_id, h.title, h.published_at, keyword, order),
            )
    if not dry_run and d.keywords:
        store.set_state(conn, POSITION_KEY, str((position + result.searches) % len(d.keywords)))
    result.hits = len(hits)

    seen = store.seen_candidate_ids(conn, hits)
    result.already_seen = len(seen)
    cands = [c for vid, c in hits.items() if vid not in seen]
    result.candidates = cands
    if not cands:
        return result

    # 2. Enrich ---------------------------------------------------------------------
    details = fetch_video_details(client, [c.video_id for c in cands], guard)
    for c in cands:
        v = details.get(c.video_id)
        if v is None:
            continue
        try:
            c.duration_seconds = parse_duration(v.duration)
        except ValueError:
            c.duration_seconds = 0  # odd duration: treat as not a Short
        c.views, c.title, c.channel_id = v.views, v.title or c.title, v.channel_id
        c.description, c.language = v.description, v.language

    curated = {ch.id for _, ch in cfg.channels.all_channels()}
    # Curated channels are dropped anyway; don't spend units on their stats.
    channel_ids = sorted({c.channel_id for c in cands} - curated)
    cached = store.discovered_channel_rows(conn, channel_ids)
    fresh_after = (now - timedelta(days=d.channel_stats_cache_days)).isoformat(timespec="seconds")
    stale = [
        cid
        for cid in channel_ids
        if cid not in cached or (cached[cid]["stats_fetched_at"] or "") < fresh_after
    ]
    fetched = channels_by_id(client, stale, guard) if stale else {}
    stats: dict[str, tuple[str, int | None, int | None]] = {
        cid: (r["title"], r["subscribers"], r["video_count"])
        for cid, r in cached.items()
        if cid not in stale
    }
    for cid, ch in fetched.items():
        stats[cid] = (ch.title, ch.subscribers, ch.video_count)
        if not dry_run:
            store.upsert_discovered_channel(
                conn, cid, ch.title, ch.handle, ch.subscribers, ch.video_count
            )
    for c in cands:
        if c.channel_id in stats:
            c.channel_title, c.subscribers, c.video_count = stats[c.channel_id]
        c.outlier_score = outlier_score(c.views, c.subscribers)

    # 3. Filter ---------------------------------------------------------------------
    status = {cid: r["status"] for cid, r in cached.items()}
    for c in cands:
        c.drop_reason = check_candidate(
            c, d, cfg.settings.max_short_seconds, curated, status, set(stats), vocab
        )
    since = (now - timedelta(days=REPOST_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
    stored = [
        (r["title"] or "", r["channel_id"], r["published_at"] or "")
        for r in store.recent_titles(conn, since)
    ]
    mark_reposts(cands, stored)

    # 4. Rank and cap ---------------------------------------------------------------
    day_start = _day_start_utc(day)
    rank_and_cap(
        cands,
        store.channels_kept_since(conn, day_start),
        store.kept_count_since(conn, day_start),
        d.max_candidates_per_day,
    )

    if not dry_run:
        found_at = store.now_iso()
        store.save_candidates(
            conn,
            [
                {
                    "video_id": c.video_id,
                    "channel_id": c.channel_id,
                    "keyword": c.keyword,
                    "search_order": c.search_order,
                    "title": c.title,
                    "published_at": c.published_at,
                    "duration_seconds": c.duration_seconds,
                    "views": c.views,
                    "subscribers": c.subscribers,
                    "outlier_score": c.outlier_score,
                    "drop_reason": c.drop_reason,
                    "found_at": found_at,
                    "description": c.description,
                    "language": c.language,
                }
                for c in cands
            ],
        )
    return result


# --- report ------------------------------------------------------------------------

REPORT_COLUMNS = [
    "kept",
    "drop_reason",
    "outlier_score",
    "views",
    "subscribers",
    "video_count",
    "duration_seconds",
    "language",
    "channel_title",
    "title",
    "keyword",
    "search_order",
    "published_at",
    "url",
    "video_id",
    "channel_id",
]


def _num(n: int | None) -> str:
    return "?" if n is None else f"{n:,}"


def render_report(result: DiscoveryResult, show_dropped: int = 3) -> str:
    """Human-readable summary: kept candidates in full, dropped ones grouped by reason."""
    mode = "DRY RUN - nothing saved, keyword rotation not advanced" if result.dry_run else "saved"
    lines = [
        f"Discovery ({mode})",
        f"  searches: {result.searches} ({', '.join(result.keywords[: result.searches]) or '-'})",
        f"  unique Shorts found: {result.hits}, already judged before: {result.already_seen}, "
        f"judged now: {len(result.candidates)}",
    ]
    if result.stopped:
        lines.append(f"  stopped early: {result.stopped}")
    kept = result.kept
    lines += ["", f"KEPT ({len(kept)})"]
    for i, c in enumerate(kept, 1):
        lines.append(
            f"{i:>3}. x{c.outlier_score:.1f}  {_num(c.views)} views / {_num(c.subscribers)} subs"
            f"  [{c.keyword}]  {c.channel_title}: {c.title}"
        )
        lines.append(f"       {c.url}")
    if not kept:
        lines.append("  (none)")
    counts = result.drop_counts()
    lines += ["", f"DROPPED ({sum(counts.values())})"]
    for reason, n in counts.items():
        lines.append(f"  {reason}: {n}")
        examples = [c for c in result.candidates if c.drop_reason == reason][:show_dropped]
        for c in examples:
            lines.append(
                f"      - {_num(c.views)} views / {_num(c.subscribers)} subs  "
                f"{c.channel_title or c.channel_id}: {c.title[:70]}"
            )
    return "\n".join(lines)


def write_report_csv(result: DiscoveryResult, output_dir: Path, day: str) -> Path:
    """Every candidate with its numbers and reason, kept first: for reviewing the filters."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"discovery-{day}{'-dryrun' if result.dry_run else ''}.csv"
    rows = sorted(
        result.candidates,
        key=lambda c: (
            not c.kept,
            DROP_REASONS.index(c.drop_reason) if c.drop_reason else -1,
            -(c.outlier_score or 0),
        ),
    )
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REPORT_COLUMNS)
        w.writeheader()
        for c in rows:
            row = {k: getattr(c, k) for k in REPORT_COLUMNS if k not in ("kept", "url")}
            score = c.outlier_score
            w.writerow(
                row
                | {
                    "kept": c.kept,
                    "url": c.url,
                    "outlier_score": None if score is None else round(score, 2),
                }
            )
    return path
