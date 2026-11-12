"""SQLite storage: schema migrations and the queries the pipeline needs.

Migrations are a numbered list. SQLite's built-in ``PRAGMA user_version`` records how many
have been applied, so each one runs exactly once per database. Never edit or reorder an
existing migration; append a new one instead. Never drop tables that hold data.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from shorts_digest.config import PROJECT_ROOT

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "digest.db"

# Video lifecycle (videos.status):
#   new               -> a Short, waiting for transcript (phase 3)
#   text_ready        -> has text, waiting for extraction (phase 4)
#   extracted         -> extraction stored, waiting for the filter (phase 5)
#   accepted          -> passed the filter; eligible for a digest
#   rejected          -> dropped; see reject_reason ("not_short", "low_usefulness",
#                        "promotional", "duplicate_concept")
#   extraction_failed -> extractor raised on this item
MIGRATIONS: list[str] = [
    # 1: initial schema (CLAUDE.md section 8) + status / reject_reason on videos
    """
    CREATE TABLE channels (
        channel_id          TEXT PRIMARY KEY,
        title               TEXT NOT NULL,
        topic               TEXT NOT NULL,
        uploads_playlist_id TEXT,
        added_at            TEXT NOT NULL
    );
    CREATE TABLE videos (
        video_id         TEXT PRIMARY KEY,
        channel_id       TEXT NOT NULL REFERENCES channels(channel_id),
        title            TEXT,
        description      TEXT,
        published_at     TEXT,
        duration_seconds INTEGER,
        is_short         INTEGER,
        url              TEXT,
        text_source      TEXT,
        transcript       TEXT,
        fetched_at       TEXT NOT NULL,
        status           TEXT NOT NULL DEFAULT 'new',
        reject_reason    TEXT
    );
    CREATE INDEX idx_videos_status ON videos(status);
    CREATE TABLE extractions (
        video_id       TEXT PRIMARY KEY REFERENCES videos(video_id),
        concept        TEXT,
        method_or_tool TEXT,
        topic          TEXT,
        summary        TEXT,
        difficulty     TEXT,
        usefulness     INTEGER,
        is_promotional INTEGER,
        confidence     TEXT,
        next_step      TEXT,
        model          TEXT,
        prompt_version TEXT,
        created_at     TEXT NOT NULL
    );
    CREATE TABLE runs (
        run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        command        TEXT,
        started_at     TEXT NOT NULL,
        finished_at    TEXT,
        status         TEXT NOT NULL DEFAULT 'running',
        videos_found   INTEGER,
        shorts_kept    INTEGER,
        quota_estimate INTEGER,
        error          TEXT
    );
    CREATE TABLE digest_items (
        run_id   INTEGER NOT NULL REFERENCES runs(run_id),
        video_id TEXT NOT NULL REFERENCES videos(video_id),
        rank     INTEGER NOT NULL,
        PRIMARY KEY (run_id, video_id)
    );
    """,
    # 2: phase 3 - remember which caption language was used and why a transcript wasn't
    """
    ALTER TABLE videos ADD COLUMN transcript_language TEXT;
    ALTER TABLE videos ADD COLUMN transcript_error TEXT;
    """,
    # 3: phase 4 - rule-based extractor explains each score
    """
    ALTER TABLE extractions ADD COLUMN reasons TEXT;
    """,
    # 4: phase 6 - per-day quota ledger; which stage a failed run died in; stage counts
    """
    CREATE TABLE quota_usage (
        day    TEXT NOT NULL,            -- quota day, YYYY-MM-DD in Pacific time
        source TEXT NOT NULL,            -- curated | discovery | resolve
        method TEXT NOT NULL,            -- e.g. playlistItems.list
        calls  INTEGER NOT NULL DEFAULT 0,
        units  INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, source, method)
    );
    ALTER TABLE runs ADD COLUMN stage TEXT;
    ALTER TABLE runs ADD COLUMN summary TEXT;
    """,
    # 5: phase 8 - discovery (search -> enrich -> filter -> rank); see CLAUDE.md section 6a
    """
    CREATE TABLE discovered_channels (
        channel_id        TEXT PRIMARY KEY,
        title             TEXT,
        handle            TEXT,
        subscribers       INTEGER,      -- NULL = hidden
        video_count       INTEGER,
        stats_fetched_at  TEXT,
        status            TEXT NOT NULL DEFAULT 'candidate',
                          -- candidate | suggested | approved | rejected | blocked
        topic             TEXT,
        status_changed_at TEXT,
        last_suggested_at TEXT
    );
    CREATE TABLE discovery_candidates (
        video_id         TEXT PRIMARY KEY,
        channel_id       TEXT NOT NULL,
        keyword          TEXT,
        search_order     TEXT,
        title            TEXT,
        published_at     TEXT,
        duration_seconds INTEGER,
        views            INTEGER,
        subscribers      INTEGER,
        outlier_score    REAL,
        drop_reason      TEXT,          -- NULL = kept
        found_at         TEXT NOT NULL
    );
    CREATE INDEX idx_candidates_found ON discovery_candidates(found_at);
    CREATE TABLE discovery_searches (
        searched_at  TEXT NOT NULL,
        quota_day    TEXT NOT NULL,
        keyword      TEXT NOT NULL,
        search_order TEXT NOT NULL,
        results      INTEGER NOT NULL,
        units        INTEGER NOT NULL,
        dry_run      INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE discovery_state (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """,
    # 6: phase 8 follow-up - topic/language filters need these; phase 9 reuses description
    """
    ALTER TABLE discovery_candidates ADD COLUMN description TEXT;
    ALTER TABLE discovery_candidates ADD COLUMN language TEXT;
    """,
    # 7: phase 9 - discovered Shorts flow through the same pipeline, tagged by source;
    #    extractions learn style (serious/funny/skit) and has_takeaway
    """
    ALTER TABLE channels ADD COLUMN source TEXT NOT NULL DEFAULT 'curated';
    ALTER TABLE videos ADD COLUMN source TEXT NOT NULL DEFAULT 'curated';
    CREATE INDEX idx_videos_source ON videos(source, status);
    ALTER TABLE extractions ADD COLUMN style TEXT;
    ALTER TABLE extractions ADD COLUMN has_takeaway INTEGER;
    """,
]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the database (creating it if needed) and apply pending migrations."""
    db_path = db_path or DEFAULT_DB_PATH  # looked up at call time so tests can redirect it
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
        # executescript() commits on its own, so put BEGIN/COMMIT inside the script:
        # the schema change and the version bump then succeed or fail together.
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")


# --- channels ----------------------------------------------------------------------


def upsert_channel(conn: sqlite3.Connection, channel_id: str, title: str, topic: str) -> None:
    """Insert a channel or refresh its title/topic (config is the source of truth)."""
    with conn:
        conn.execute(
            """
            INSERT INTO channels (channel_id, title, topic, added_at, source)
            VALUES (?, ?, ?, ?, 'curated')
            ON CONFLICT(channel_id) DO UPDATE SET title = excluded.title, topic = excluded.topic,
                source = 'curated'
            """,
            (channel_id, title, topic, now_iso()),
        )


def get_uploads_playlists(conn: sqlite3.Connection) -> dict[str, str]:
    """channel_id -> cached uploads playlist ID (only channels already resolved)."""
    rows = conn.execute(
        "SELECT channel_id, uploads_playlist_id FROM channels WHERE uploads_playlist_id IS NOT NULL"
    )
    return {r["channel_id"]: r["uploads_playlist_id"] for r in rows}


def set_uploads_playlist(conn: sqlite3.Connection, channel_id: str, playlist_id: str) -> None:
    with conn:
        conn.execute(
            "UPDATE channels SET uploads_playlist_id = ? WHERE channel_id = ?",
            (playlist_id, channel_id),
        )


# --- videos ------------------------------------------------------------------------


@dataclass
class VideoRow:
    video_id: str
    channel_id: str
    title: str
    description: str
    published_at: str
    duration_seconds: int
    is_short: bool
    status: str = "new"
    reject_reason: str | None = None

    @property
    def url(self) -> str:
        if self.is_short:
            return f"https://www.youtube.com/shorts/{self.video_id}"
        return f"https://www.youtube.com/watch?v={self.video_id}"


def known_video_ids(conn: sqlite3.Connection, video_ids: Iterable[str]) -> set[str]:
    ids = list(video_ids)
    known: set[str] = set()
    # SQLite limits the number of "?" placeholders, so check in chunks.
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(f"SELECT video_id FROM videos WHERE video_id IN ({marks})", chunk)
        known.update(r["video_id"] for r in rows)
    return known


def insert_videos(conn: sqlite3.Connection, videos: Iterable[VideoRow]) -> int:
    """Insert new videos. Existing IDs are left untouched (video-level dedup)."""
    fetched_at = now_iso()
    with conn:
        cur = conn.executemany(
            """
            INSERT OR IGNORE INTO videos (video_id, channel_id, title, description, published_at,
                duration_seconds, is_short, url, fetched_at, status, reject_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    v.video_id,
                    v.channel_id,
                    v.title,
                    v.description,
                    v.published_at,
                    v.duration_seconds,
                    int(v.is_short),
                    v.url,
                    fetched_at,
                    v.status,
                    v.reject_reason,
                )
                for v in videos
            ],
        )
    return cur.rowcount


# --- runs --------------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, command: str) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO runs (command, started_at) VALUES (?, ?)", (command, now_iso())
        )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    *,
    videos_found: int = 0,
    shorts_kept: int = 0,
    quota_estimate: int = 0,
    error: str | None = None,
    stage: str | None = None,
    summary: dict | None = None,
) -> None:
    """Close a run. ``stage`` = where a failed run stopped; ``summary`` = per-stage counts."""
    import json

    with conn:
        conn.execute(
            """
            UPDATE runs SET finished_at = ?, status = ?, videos_found = ?, shorts_kept = ?,
                quota_estimate = ?, error = ?, stage = ?, summary = ?
            WHERE run_id = ?
            """,
            (
                now_iso(),
                status,
                videos_found,
                shorts_kept,
                quota_estimate,
                error,
                stage,
                json.dumps(summary) if summary is not None else None,
                run_id,
            ),
        )


def mark_interrupted_runs(conn: sqlite3.Connection) -> int:
    """Close runs left as 'running' by a crash, Ctrl-C or a cancelled CI job.

    Call this only when starting a new run (the daily job never runs twice at once).
    Their work is not lost: every video keeps its status, so the new run carries on.
    """
    with conn:
        cur = conn.execute(
            "UPDATE runs SET status = 'failed', error = 'interrupted', finished_at = ? "
            "WHERE status = 'running'",
            (now_iso(),),
        )
    return cur.rowcount


# --- quota -------------------------------------------------------------------------


def add_quota_usage(conn: sqlite3.Connection, day: str, source: str, method: str, units: int):
    with conn:
        conn.execute(
            """
            INSERT INTO quota_usage (day, source, method, calls, units) VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(day, source, method)
            DO UPDATE SET calls = calls + 1, units = units + excluded.units
            """,
            (day, source, method, units),
        )


def quota_used(conn: sqlite3.Connection, day: str, source: str | None = None) -> int:
    sql, params = "SELECT coalesce(sum(units), 0) FROM quota_usage WHERE day = ?", [day]
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    return int(conn.execute(sql, params).fetchone()[0])


def quota_breakdown(conn: sqlite3.Connection, day: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT source, method, calls, units FROM quota_usage WHERE day = ? "
        "ORDER BY source, units DESC",
        (day,),
    ).fetchall()


def last_successful_fetch(conn: sqlite3.Connection) -> datetime | None:
    """Start time of the last complete (not --limit, not failed) discovery run."""
    row = conn.execute(
        "SELECT started_at FROM runs WHERE status = 'success' AND command IN ('fetch', 'run') "
        "ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    return datetime.fromisoformat(row["started_at"]) if row else None


def videos_needing_text(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Shorts discovered but not yet given text (status 'new'), newest first."""
    sql = (
        "SELECT video_id, title, description FROM videos "
        "WHERE status = 'new' AND is_short = 1 ORDER BY published_at DESC"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def save_text(
    conn: sqlite3.Connection,
    video_id: str,
    text_source: str,
    transcript: str | None,
    language: str | None,
    error: str | None,
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE videos SET text_source = ?, transcript = ?, transcript_language = ?,
                transcript_error = ?, status = 'text_ready'
            WHERE video_id = ?
            """,
            (text_source, transcript, language, error, video_id),
        )


def add_discovered_video(
    conn: sqlite3.Connection,
    video_id: str,
    channel_id: str,
    channel_title: str,
    topic_hint: str,
    title: str,
    description: str,
    published_at: str,
    duration_seconds: int,
) -> None:
    """Queue a kept discovery candidate for the normal text -> extract -> filter path.

    The channel row is only created if missing: a curated channel is never downgraded.
    ``topic_hint`` is a starting topic for the extractor's tie-break (see discovery.py).
    """
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO channels (channel_id, title, topic, added_at, source) "
            "VALUES (?, ?, ?, ?, 'discovery')",
            (channel_id, channel_title, topic_hint, now_iso()),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO videos (video_id, channel_id, title, description, published_at,
                duration_seconds, is_short, url, fetched_at, status, source)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'new', 'discovery')
            """,
            (
                video_id,
                channel_id,
                title,
                description,
                published_at,
                duration_seconds,
                f"https://www.youtube.com/shorts/{video_id}",
                now_iso(),
            ),
        )


def videos_needing_extraction(
    conn: sqlite3.Connection, limit: int | None = None
) -> list[sqlite3.Row]:
    """Shorts with text but no extraction yet, with their channel's topic."""
    sql = """
        SELECT v.video_id, v.title, v.description, v.transcript, v.text_source,
               c.topic AS channel_topic
        FROM videos v JOIN channels c USING (channel_id)
        WHERE v.status = 'text_ready'
        ORDER BY v.published_at DESC
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql).fetchall()


def save_extraction(
    conn: sqlite3.Connection, video_id: str, data: dict, model: str, version: str
) -> None:
    """Store one extraction and mark the video extracted (one transaction)."""
    import json

    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO extractions (video_id, concept, method_or_tool, topic, summary,
                difficulty, usefulness, is_promotional, confidence, next_step, model,
                prompt_version, created_at, reasons, style, has_takeaway)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                video_id,
                data["concept"],
                data["method_or_tool"],
                data["topic"],
                data["summary"],
                data["difficulty"],
                data["usefulness"],
                int(data["is_promotional"]),
                data["confidence"],
                data["next_step"],
                model,
                version,
                now_iso(),
                json.dumps(data.get("reasons", [])),
                data.get("style", "serious"),
                int(data.get("has_takeaway", True)),
            ),
        )
        conn.execute("UPDATE videos SET status = 'extracted' WHERE video_id = ?", (video_id,))


# --- filter + digest ---------------------------------------------------------------


def videos_to_filter(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Extracted, not yet filtered. Best first, so a duplicate keeps the better copy."""
    return conn.execute(
        """
        SELECT v.video_id, e.concept, e.usefulness, e.is_promotional,
               coalesce(e.has_takeaway, 1) AS has_takeaway
        FROM videos v JOIN extractions e USING (video_id)
        WHERE v.status = 'extracted'
        ORDER BY e.usefulness DESC, v.published_at DESC
        """
    ).fetchall()


def accepted_concepts_since(conn: sqlite3.Connection, since: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT e.concept FROM extractions e JOIN videos v USING (video_id)
        WHERE v.status = 'accepted' AND e.created_at >= ?
        """,
        (since,),
    )
    return [r["concept"] for r in rows]


def set_status(
    conn: sqlite3.Connection, video_id: str, status: str, reason: str | None = None
) -> None:
    with conn:
        conn.execute(
            "UPDATE videos SET status = ?, reject_reason = ? WHERE video_id = ?",
            (status, reason, video_id),
        )


_NOT_DIGESTED = "AND v.video_id NOT IN (SELECT video_id FROM digest_items)"


def digest_candidates(
    conn: sqlite3.Connection, since: str, exclude_digested: bool, source: str = "curated"
) -> list[sqlite3.Row]:
    """Accepted Shorts published since ``since`` with everything the digest shows.

    For discovery items it also brings the numbers "New voices" shows (from the candidate
    row: subscribers and views when found, and the keyword that found it).
    """
    return conn.execute(
        f"""
        SELECT v.video_id, e.topic, e.concept, e.summary, e.method_or_tool, e.difficulty,
               e.usefulness, e.confidence, e.next_step, v.url, c.title AS channel,
               v.published_at, v.text_source, coalesce(e.style, 'serious') AS style,
               v.source AS section, d.subscribers, d.views, d.keyword, v.channel_id
        FROM videos v JOIN extractions e USING (video_id) JOIN channels c USING (channel_id)
        LEFT JOIN discovery_candidates d USING (video_id)
        WHERE v.status = 'accepted' AND v.published_at >= ? AND v.source = ?
        {_NOT_DIGESTED if exclude_digested else ""}
        """,
        (since, source),
    ).fetchall()


def shorts_pool_counts(
    conn: sqlite3.Connection, since: str, exclude_digested: bool
) -> tuple[int, int]:
    """(number of Shorts, number of channels) published since ``since``: the digest header."""
    row = conn.execute(
        f"""
        SELECT count(*) AS n, count(DISTINCT v.channel_id) AS ch FROM videos v
        WHERE v.is_short = 1 AND v.published_at >= ? AND v.source = 'curated'
        {_NOT_DIGESTED if exclude_digested else ""}
        """,
        (since,),
    ).fetchone()
    return row["n"], row["ch"]


def unrecord_digest(conn: sqlite3.Connection, run_id: int, suggested: list[str]) -> None:
    """Undo record_digest when the email could not be sent, so the next run resends."""
    with conn:
        conn.execute("DELETE FROM digest_items WHERE run_id = ?", (run_id,))
        conn.executemany(
            "UPDATE discovered_channels SET last_suggested_at = NULL WHERE channel_id = ?",
            [(cid,) for cid in suggested],
        )


def record_digest(conn: sqlite3.Connection, run_id: int, ranked: list[tuple[str, int]]) -> None:
    """Remember which videos went out in which run, so they are never sent twice."""
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO digest_items (run_id, video_id, rank) VALUES (?, ?, ?)",
            [(run_id, vid, rank) for vid, rank in ranked],
        )


# --- discovery ---------------------------------------------------------------------


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM discovery_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO discovery_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def log_search(
    conn: sqlite3.Connection,
    quota_day: str,
    keyword: str,
    order: str,
    results: int,
    units: int,
    dry_run: bool,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO discovery_searches VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), quota_day, keyword, order, results, units, int(dry_run)),
        )


def searches_on(conn: sqlite3.Connection, quota_day: str) -> int:
    """Searches already made on this quota day (dry runs too: they spent real quota)."""
    return conn.execute(
        "SELECT count(*) FROM discovery_searches WHERE quota_day = ?", (quota_day,)
    ).fetchone()[0]


def discovered_channel_rows(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, sqlite3.Row]:
    ids = list(ids)
    out: dict[str, sqlite3.Row] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT * FROM discovered_channels WHERE channel_id IN ({marks})", chunk
        ):
            out[r["channel_id"]] = r
    return out


def upsert_discovered_channel(
    conn: sqlite3.Connection,
    channel_id: str,
    title: str,
    handle: str | None,
    subscribers: int | None,
    video_count: int | None,
) -> None:
    """Refresh a channel's stats. Never touches its status (that is the owner's)."""
    with conn:
        conn.execute(
            """
            INSERT INTO discovered_channels
                (channel_id, title, handle, subscribers, video_count, stats_fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET title = excluded.title,
                handle = excluded.handle, subscribers = excluded.subscribers,
                video_count = excluded.video_count, stats_fetched_at = excluded.stats_fetched_at
            """,
            (channel_id, title, handle, subscribers, video_count, now_iso()),
        )


def seen_candidate_ids(conn: sqlite3.Connection, video_ids: Iterable[str]) -> set[str]:
    """Video IDs already judged by discovery, or already stored as curated videos."""
    ids = list(video_ids)
    seen: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        for table in ("discovery_candidates", "videos"):
            rows = conn.execute(f"SELECT video_id FROM {table} WHERE video_id IN ({marks})", chunk)
            seen.update(r["video_id"] for r in rows)
    return seen


def recent_titles(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    """(title, channel_id, published_at) of curated videos and discovery candidates since
    ``since``: what a new candidate is compared with to spot reposts."""
    return conn.execute(
        """
        SELECT title, channel_id, published_at FROM videos WHERE published_at >= ?
        UNION ALL
        SELECT title, channel_id, published_at FROM discovery_candidates WHERE published_at >= ?
        """,
        (since, since),
    ).fetchall()


def channels_kept_since(conn: sqlite3.Connection, since: str) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT channel_id FROM discovery_candidates "
        "WHERE drop_reason IS NULL AND found_at >= ?",
        (since,),
    )
    return {r["channel_id"] for r in rows}


def kept_count_since(conn: sqlite3.Connection, since: str) -> int:
    return conn.execute(
        "SELECT count(*) FROM discovery_candidates WHERE drop_reason IS NULL AND found_at >= ?",
        (since,),
    ).fetchone()[0]


def save_candidates(conn: sqlite3.Connection, rows: list[dict]) -> None:
    with conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO discovery_candidates (video_id, channel_id, keyword,
                search_order, title, published_at, duration_seconds, views, subscribers,
                outlier_score, drop_reason, found_at, description, language)
            VALUES (:video_id, :channel_id, :keyword, :search_order, :title, :published_at,
                :duration_seconds, :views, :subscribers, :outlier_score, :drop_reason, :found_at,
                :description, :language)
            """,
            rows,
        )


# --- promotion of discovered channels ----------------------------------------------


def promote_channels(conn: sqlite3.Connection, min_usefulness: int, since: str, needed: int):
    """candidate -> suggested once a channel has ``needed`` accepted Shorts at or above
    ``min_usefulness`` since ``since``. Rejected/blocked/approved channels are never touched,
    so a rejected channel is never suggested again. Returns the newly suggested IDs."""
    rows = conn.execute(
        """
        SELECT dc.channel_id FROM discovered_channels dc
        JOIN videos v ON v.channel_id = dc.channel_id AND v.source = 'discovery'
        JOIN extractions e ON e.video_id = v.video_id
        WHERE dc.status = 'candidate' AND v.status = 'accepted'
          AND e.usefulness >= ? AND e.created_at >= ?
        GROUP BY dc.channel_id HAVING count(*) >= ?
        """,
        (min_usefulness, since, needed),
    ).fetchall()
    ids = [r["channel_id"] for r in rows]
    with conn:
        conn.executemany(
            "UPDATE discovered_channels SET status = 'suggested', status_changed_at = ? "
            "WHERE channel_id = ?",
            [(now_iso(), cid) for cid in ids],
        )
    return ids


def channel_suggestions(
    conn: sqlite3.Connection, shown_before: str | None = None
) -> list[sqlite3.Row]:
    """Suggested channels with their useful-Short count and most common topic.

    ``shown_before``: only channels not shown in a digest since then (weekly reminder).
    """
    extra = ""
    params: list = []
    if shown_before is not None:
        extra = "AND (dc.last_suggested_at IS NULL OR dc.last_suggested_at < ?)"
        params.append(shown_before)
    return conn.execute(
        f"""
        SELECT dc.channel_id, dc.title, dc.handle, dc.subscribers, dc.status,
               dc.last_suggested_at,
               (SELECT count(*) FROM videos v WHERE v.channel_id = dc.channel_id
                  AND v.source = 'discovery' AND v.status = 'accepted') AS accepted,
               (SELECT e.topic FROM videos v JOIN extractions e USING (video_id)
                  WHERE v.channel_id = dc.channel_id AND v.status = 'accepted'
                  GROUP BY e.topic ORDER BY count(*) DESC LIMIT 1) AS topic
        FROM discovered_channels dc
        WHERE dc.status = 'suggested' {extra}
        ORDER BY accepted DESC, dc.subscribers DESC
        """,
        params,
    ).fetchall()


def mark_suggested_shown(conn: sqlite3.Connection, channel_ids: list[str]) -> None:
    with conn:
        conn.executemany(
            "UPDATE discovered_channels SET last_suggested_at = ? WHERE channel_id = ?",
            [(now_iso(), cid) for cid in channel_ids],
        )


def get_discovered_channel(conn: sqlite3.Connection, channel_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM discovered_channels WHERE channel_id = ?", (channel_id,)
    ).fetchone()


def set_channel_status(
    conn: sqlite3.Connection, channel_id: str, status: str, topic: str | None = None
) -> None:
    with conn:
        conn.execute(
            "UPDATE discovered_channels SET status = ?, status_changed_at = ?, "
            "topic = coalesce(?, topic) WHERE channel_id = ?",
            (status, now_iso(), topic, channel_id),
        )
