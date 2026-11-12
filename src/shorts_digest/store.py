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
#   text_ready        -> has text, waiting for the LLM (phase 4)
#   extracted         -> LLM result stored
#   rejected          -> dropped; see reject_reason (e.g. "not_short", "low_usefulness")
#   extraction_failed -> LLM output invalid twice
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
        with conn:  # one transaction per migration
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {version}")


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
