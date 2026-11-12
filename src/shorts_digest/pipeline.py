"""Pipeline stages, wired together. Each stage reads/writes SQLite so a failed run can resume.

Phase 2: fetch stage (discover new uploads, classify Shorts, store everything).
Phase 3: text stage (transcript, or title + description as a fallback).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from googleapiclient.errors import HttpError

from shorts_digest import store
from shorts_digest.config import AppConfig
from shorts_digest.shorts import check_shorts_url, is_short_by_duration, parse_duration
from shorts_digest.transcripts import TextResult, Throttle, fetch_text
from shorts_digest.youtube import (
    PlaylistItem,
    QuotaTracker,
    fetch_video_details,
    list_recent_uploads,
    resolve_uploads_playlists,
)

log = logging.getLogger(__name__)

# Re-check a few hours before the last run's start: a video can show up in the playlist a
# little after its publish time. Duplicates are free because video_id is the primary key.
OVERLAP = timedelta(hours=6)


@dataclass
class FetchResult:
    videos_found: int = 0  # new (not yet stored) uploads discovered
    shorts_kept: int = 0
    channels_failed: int = 0
    complete: bool = True  # False when --limit left some videos for later


def _since(conn: sqlite3.Connection, lookback_days: int, now: datetime) -> datetime:
    last = store.last_successful_fetch(conn)
    if last is None:
        return now - timedelta(days=lookback_days)
    return last - OVERLAP


def fetch_stage(
    cfg: AppConfig,
    conn: sqlite3.Connection,
    client: Any,
    quota: QuotaTracker,
    limit: int | None = None,
    now: datetime | None = None,
) -> FetchResult:
    now = now or datetime.now(UTC)
    settings = cfg.settings
    result = FetchResult()

    # 1. Make sure every configured channel is stored, and resolve missing playlist IDs once.
    for topic, ch in cfg.channels.all_channels():
        store.upsert_channel(conn, ch.id, ch.name, topic)
    cached = store.get_uploads_playlists(conn)
    configured = [ch.id for _, ch in cfg.channels.all_channels()]
    missing = [cid for cid in configured if cid not in cached]
    if missing:
        resolved = resolve_uploads_playlists(client, missing, quota)
        for cid, pid in resolved.items():
            store.set_uploads_playlist(conn, cid, pid)
            cached[cid] = pid
        for cid in set(missing) - set(resolved):
            log.warning("Channel %s not found by the API; check channels.yaml", cid)

    # 2. List recent uploads per channel. One broken channel must not stop the run.
    since = _since(conn, settings.lookback_days, now)
    log.info("Looking for uploads since %s", since.isoformat(timespec="minutes"))
    discovered: list[PlaylistItem] = []
    for cid in configured:
        pid = cached.get(cid)
        if not pid:
            result.channels_failed += 1
            continue
        try:
            discovered.extend(list_recent_uploads(client, pid, since, quota))
        except HttpError as err:  # fatal errors (quota/key) are not HttpError; they propagate
            log.warning("Skipping channel %s: HTTP %s", cid, err.status_code)
            result.channels_failed += 1

    # 3. Only look up videos we haven't stored before (saves quota, keeps reruns idempotent).
    known = store.known_video_ids(conn, (p.video_id for p in discovered))
    new_items = sorted(
        (p for p in discovered if p.video_id not in known),
        key=lambda p: p.published_at,
        reverse=True,
    )
    result.videos_found = len(new_items)
    if limit is not None and len(new_items) > limit:
        new_items = new_items[:limit]
        result.complete = False  # the rest are picked up by the next full run

    # 4. Durations in batches of 50, then classify.
    details = fetch_video_details(client, [p.video_id for p in new_items], quota)
    rows: list[store.VideoRow] = []
    for item in new_items:
        d = details.get(item.video_id)
        if d is None:  # deleted or made private between the two calls
            continue
        try:
            seconds = parse_duration(d.duration)
        except ValueError:
            log.warning(
                "Video %s: odd duration %r, treating as not a Short", d.video_id, d.duration
            )
            seconds = 0
        short = is_short_by_duration(seconds, settings.max_short_seconds)
        if short and settings.verify_shorts_url:
            verdict = check_shorts_url(d.video_id)
            if verdict is False:
                short = False
            # None = unknown -> keep the duration-based answer
        rows.append(
            store.VideoRow(
                video_id=d.video_id,
                channel_id=d.channel_id,
                title=d.title,
                description=d.description,
                published_at=d.published_at,
                duration_seconds=seconds,
                is_short=short,
                status="new" if short else "rejected",
                reject_reason=None if short else "not_short",
            )
        )
    store.insert_videos(conn, rows)
    result.shorts_kept = sum(r.is_short for r in rows)
    return result


# --- text stage --------------------------------------------------------------------

TRANSCRIPT_INTERVAL = 1.0  # seconds between transcript requests
MAX_CONSECUTIVE_BLOCKS = 3  # after this, assume our IP is blocked and stop asking


@dataclass
class TextStageResult:
    processed: int = 0
    transcripts: int = 0
    metadata: int = 0
    blocked: bool = False
    errors: dict[str, int] | None = None


def text_stage(
    conn: sqlite3.Connection,
    limit: int | None = None,
    api: Any | None = None,
    interval: float = TRANSCRIPT_INTERVAL,
) -> TextStageResult:
    """Give every new Short some text. Never fails because of a transcript problem."""
    result = TextStageResult(errors={})
    throttle = Throttle(interval)
    consecutive_blocks = 0

    for row in store.videos_needing_text(conn, limit):
        if consecutive_blocks >= MAX_CONSECUTIVE_BLOCKS:
            # Keep going with metadata only; hammering a blocked endpoint won't help.
            text = TextResult("metadata", None, error="skipped_ip_blocked")
        else:
            throttle.wait()
            text = fetch_text(row["video_id"], api)
            consecutive_blocks = consecutive_blocks + 1 if text.blocked else 0
            if consecutive_blocks == MAX_CONSECUTIVE_BLOCKS:
                result.blocked = True
                log.warning(
                    "Transcript requests look blocked (common on cloud IPs); "
                    "using title + description for the rest of this run."
                )

        store.save_text(
            conn, row["video_id"], text.text_source, text.transcript, text.language, text.error
        )
        result.processed += 1
        if text.text_source == "transcript":
            result.transcripts += 1
        else:
            result.metadata += 1
            reason = text.error or "unknown"
            result.errors[reason] = result.errors.get(reason, 0) + 1
    return result
