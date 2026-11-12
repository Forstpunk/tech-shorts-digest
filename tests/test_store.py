from datetime import datetime

from shorts_digest import store


def test_migrations_run_once_and_are_idempotent(tmp_path):
    db = tmp_path / "t.db"
    conn = store.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(store.MIGRATIONS)
    conn.close()
    conn = store.connect(db)  # second open must not fail or recreate tables
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"channels", "videos", "extractions", "runs", "digest_items"} <= tables


def test_insert_videos_ignores_duplicates(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    store.upsert_channel(conn, "UC" + "a" * 22, "A", "python")
    row = store.VideoRow("vid1", "UC" + "a" * 22, "t", "d", "2026-11-10T00:00:00Z", 30, True)
    assert store.insert_videos(conn, [row]) == 1
    assert store.insert_videos(conn, [row]) == 0
    assert store.known_video_ids(conn, ["vid1", "vid2"]) == {"vid1"}
    url = conn.execute("SELECT url FROM videos").fetchone()[0]
    assert url == "https://www.youtube.com/shorts/vid1"


def test_last_successful_fetch_ignores_partial_and_failed(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    assert store.last_successful_fetch(conn) is None
    ok = store.start_run(conn, "fetch")
    store.finish_run(conn, ok, "success")
    first = store.last_successful_fetch(conn)
    assert isinstance(first, datetime)
    for status in ("partial", "failed"):
        rid = store.start_run(conn, "fetch")
        store.finish_run(conn, rid, status)
    assert store.last_successful_fetch(conn) == first
