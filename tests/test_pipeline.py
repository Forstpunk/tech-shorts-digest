"""Fetch stage end to end against a fake YouTube client and a temp SQLite file."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import CH_A, CH_B, FakeYouTube, http_error

from shorts_digest import store
from shorts_digest.config import load_config
from shorts_digest.pipeline import fetch_stage
from shorts_digest.youtube import QuotaTracker, YouTubeFatalError

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
REAL_CONFIG = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "settings.yaml").write_text((REAL_CONFIG / "settings.yaml").read_text())
    (tmp_path / "channels.yaml").write_text(
        f"topics:\n  python:\n    - {{id: {CH_A}, name: A}}\n"
        f"  data_engineering:\n    - {{id: {CH_B}, name: B}}\n"
    )
    return load_config(tmp_path, env_file=tmp_path / "none.env")


@pytest.fixture
def conn(tmp_path):
    return store.connect(tmp_path / "t.db")


def pl(video_id, ts):
    return {"videoId": video_id, "videoPublishedAt": ts}


def vid(channel, duration, ts="2026-11-11T10:00:00Z"):
    return {"channelId": channel, "duration": duration, "publishedAt": ts}


def make_fake():
    return FakeYouTube(
        channels={CH_A: "UUa", CH_B: "UUb"},
        playlists={
            "UUa": [
                pl("a_short", "2026-11-11T10:00:00Z"),
                pl("a_long", "2026-11-10T10:00:00Z"),
                pl("a_old", "2026-11-01T10:00:00Z"),  # older than the 3-day lookback
            ],
            "UUb": [pl("b_short", "2026-11-12T08:00:00Z")],
        },
        videos={
            "a_short": vid(CH_A, "PT45S"),
            "a_long": vid(CH_A, "PT12M3S"),
            "a_old": vid(CH_A, "PT30S"),
            "b_short": vid(CH_B, "PT2M59S"),
        },
    )


def statuses(conn):
    return {
        r["video_id"]: (r["status"], r["reject_reason"])
        for r in conn.execute("SELECT * FROM videos")
    }


def test_first_run_classifies_and_stores(cfg, conn):
    yt, quota = make_fake(), QuotaTracker()
    result = fetch_stage(cfg, conn, yt, quota, now=NOW)

    assert result.videos_found == 3 and result.shorts_kept == 2 and result.complete
    assert statuses(conn) == {
        "a_short": ("new", None),
        "b_short": ("new", None),
        "a_long": ("rejected", "not_short"),
    }
    # 1 channels.list for both channels, 1 page per playlist, 1 videos.list batch
    assert (yt.count("channels.list"), yt.count("playlistItems.list"), yt.count("videos.list")) == (
        1,
        2,
        1,
    )
    assert quota.units == 4
    assert store.get_uploads_playlists(conn) == {CH_A: "UUa", CH_B: "UUb"}


def test_second_run_uses_cache_and_skips_known_videos(cfg, conn):
    fetch_stage(cfg, conn, make_fake(), QuotaTracker(), now=NOW)
    yt = make_fake()
    result = fetch_stage(cfg, conn, yt, QuotaTracker(), now=NOW)
    assert result.videos_found == 0
    assert yt.count("channels.list") == 0  # playlist IDs came from SQLite
    assert yt.count("videos.list") == 0  # nothing new to look up


def test_limit_marks_run_incomplete(cfg, conn):
    result = fetch_stage(cfg, conn, make_fake(), QuotaTracker(), limit=1, now=NOW)
    assert result.videos_found == 3 and not result.complete
    assert list(statuses(conn)) == ["b_short"]  # newest first


def test_one_broken_channel_does_not_stop_the_run(cfg, conn):
    yt = make_fake()
    yt.playlists_data["UUa"] = http_error(404, "playlistNotFound")
    result = fetch_stage(cfg, conn, yt, QuotaTracker(), now=NOW)
    assert result.channels_failed == 1
    assert list(statuses(conn)) == ["b_short"]


def test_quota_exceeded_fails_the_run(cfg, conn):
    yt = make_fake()
    yt.playlists_data["UUa"] = http_error(403, "quotaExceeded")
    with pytest.raises(YouTubeFatalError, match="quotaExceeded"):
        fetch_stage(cfg, conn, yt, QuotaTracker(), now=NOW)


def test_paging_stops_at_first_older_video(cfg, conn):
    yt = make_fake()
    recent = [pl(f"r{i}", "2026-11-12T00:00:00Z") for i in range(3)]
    older = [pl(f"o{i}", "2026-10-01T00:00:00Z") for i in range(5)]
    yt.playlists_data["UUa"] = recent + older
    yt.page_size = 2  # forces paging: [r0,r1] [r2,o0] -> stop
    for v in recent + older:
        yt.videos_data[v["videoId"]] = vid(CH_A, "PT20S")
    fetch_stage(cfg, conn, yt, QuotaTracker(), now=NOW)
    pages_a = [
        kw for name, kw in yt.calls if name == "playlistItems.list" and kw["playlistId"] == "UUa"
    ]
    assert len(pages_a) == 2
    assert {"r0", "r1", "r2"} <= set(statuses(conn)) and "o0" not in statuses(conn)


def test_lookback_starts_from_last_successful_run(cfg, conn):
    rid = store.start_run(conn, "fetch")
    store.finish_run(conn, rid, "success")
    conn.execute("UPDATE runs SET started_at = '2026-11-12T00:00:00+00:00'")
    conn.commit()
    # since = 2026-11-11T18:00 (6h overlap): only b_short (11-12 08:00) is recent enough
    fetch_stage(cfg, conn, make_fake(), QuotaTracker(), now=NOW)
    assert set(statuses(conn)) == {"b_short"}
