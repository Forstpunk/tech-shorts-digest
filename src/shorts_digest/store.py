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
            INSERT INTO channels (channel_id, title, topic, added_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET title = excluded.title, topic = excluded.topic
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
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE runs SET finished_at = ?, status = ?, videos_found = ?, shorts_kept = ?,
                quota_estimate = ?, error = ?
            WHERE run_id = ?
            """,
            (now_iso(), status, videos_found, shorts_kept, quota_estimate, error, run_id),
        )


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
                prompt_version, created_at, reasons)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        conn.execute("UPDATE videos SET status = 'extracted' WHERE video_id = ?", (video_id,))


# --- filter + digest ---------------------------------------------------------------


def videos_to_filter(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Extracted, not yet filtered. Best first, so a duplicate keeps the better copy."""
    return conn.execute(
        """
        SELECT v.video_id, e.concept, e.usefulness, e.is_promotional
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
    conn: sqlite3.Connection, since: str, exclude_digested: bool
) -> list[sqlite3.Row]:
    """Accepted Shorts published since ``since`` with everything the digest shows."""
    return conn.execute(
        f"""
        SELECT v.video_id, e.topic, e.concept, e.summary, e.method_or_tool, e.difficulty,
               e.usefulness, e.confidence, e.next_step, v.url, c.title AS channel,
               v.published_at, v.text_source
        FROM videos v JOIN extractions e USING (video_id) JOIN channels c USING (channel_id)
        WHERE v.status = 'accepted' AND v.published_at >= ?
        {_NOT_DIGESTED if exclude_digested else ""}
        """,
        (since,),
    ).fetchall()


def shorts_pool_counts(
    conn: sqlite3.Connection, since: str, exclude_digested: bool
) -> tuple[int, int]:
    """(number of Shorts, number of channels) published since ``since``: the digest header."""
    row = conn.execute(
        f"""
        SELECT count(*) AS n, count(DISTINCT v.channel_id) AS ch FROM videos v
        WHERE v.is_short = 1 AND v.published_at >= ?
        {_NOT_DIGESTED if exclude_digested else ""}
        """,
        (since,),
    ).fetchone()
    return row["n"], row["ch"]


def record_digest(conn: sqlite3.Connection, run_id: int, ranked: list[tuple[str, int]]) -> None:
    """Remember which videos went out in which run, so they are never sent twice."""
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO digest_items (run_id, video_id, rank) VALUES (?, ?, ?)",
            [(run_id, vid, rank) for vid, rank in ranked],
        )
