"""Retries, failure recording, interrupted runs and resuming. All offline."""

from pathlib import Path

import pytest
from fakes import CH_A, CH_B, FakeYouTube, http_error, make_guard, settings_text

import shorts_digest.cli as cli
import shorts_digest.transcripts as transcripts
import shorts_digest.youtube as youtube
from shorts_digest import store
from shorts_digest.config import load_config
from shorts_digest.quota import QuotaLimitError
from shorts_digest.youtube import YouTubeFatalError, fetch_video_details

REAL_CONFIG = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    pauses: list[float] = []
    monkeypatch.setattr(youtube, "_sleep", pauses.append)
    monkeypatch.setattr(transcripts, "_sleep", pauses.append)
    return pauses


@pytest.fixture
def conn(tmp_path):
    return store.connect(tmp_path / "r.db")


def one_video():
    return FakeYouTube(
        videos={"v1": {"channelId": CH_A, "duration": "PT30S", "publishedAt": "2026-11-11T10Z"}}
    )


# --- YouTube retries ---------------------------------------------------------------


def test_transient_error_is_retried_with_backoff(conn, no_waiting):
    yt = one_video()
    yt.failures["videos.list"] = [http_error(503, "backendError"), http_error(429)]
    guard = make_guard(conn)
    assert "v1" in fetch_video_details(yt, ["v1"], guard)
    assert yt.count("videos.list") == 3
    assert no_waiting == [2.0, 4.0]
    assert guard.units == 3  # every attempt is charged


def test_network_error_is_retried(conn):
    yt = one_video()
    yt.failures["videos.list"] = [TimeoutError("slow")]
    assert "v1" in fetch_video_details(yt, ["v1"], make_guard(conn))


def test_gives_up_after_three_attempts(conn):
    yt = one_video()
    yt.failures["videos.list"] = [http_error(500)] * 3
    with pytest.raises(youtube.HttpError):
        fetch_video_details(yt, ["v1"], make_guard(conn))
    assert yt.count("videos.list") == 3


def test_rate_limit_403_is_retried_but_quota_exceeded_is_fatal(conn):
    yt = one_video()
    yt.failures["videos.list"] = [http_error(403, "rateLimitExceeded")]
    assert "v1" in fetch_video_details(yt, ["v1"], make_guard(conn))

    yt.failures["videos.list"] = [http_error(403, "quotaExceeded")]
    with pytest.raises(YouTubeFatalError):
        fetch_video_details(yt, ["v1"], make_guard(conn))


def test_plain_404_is_not_retried(conn):
    yt = one_video()
    yt.failures["videos.list"] = [http_error(404, "notFound")]
    with pytest.raises(youtube.HttpError):
        fetch_video_details(yt, ["v1"], make_guard(conn))
    assert yt.count("videos.list") == 1


def test_guard_refuses_before_sending(conn):
    yt = one_video()
    guard = make_guard(conn, daily_limit=1, reserve=0)
    fetch_video_details(yt, ["v1"], guard)
    with pytest.raises(QuotaLimitError):
        fetch_video_details(yt, ["v1"], guard)
    assert yt.count("videos.list") == 1  # the second call never reached the API


# --- transcript retry --------------------------------------------------------------


class FlakyApi:
    """Raises a network error ``fails`` times, then reports no transcript."""

    def __init__(self, fails):
        self.fails, self.calls = fails, 0

    def list(self, video_id):
        self.calls += 1
        if self.calls <= self.fails:
            raise ConnectionError("reset")
        from youtube_transcript_api import TranscriptsDisabled

        raise TranscriptsDisabled(video_id)


def test_transcript_network_error_retried_once():
    api = FlakyApi(fails=1)
    r = transcripts.fetch_text("v1", api)
    assert (api.calls, r.error) == (2, "TranscriptsDisabled")


def test_transcript_network_error_twice_falls_back():
    api = FlakyApi(fails=5)
    r = transcripts.fetch_text("v1", api)
    assert (api.calls, r.text_source, r.error) == (2, "metadata", "network:ConnectionError")


# --- recorded runs (CLI) -----------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    """Two fake channels, a fake API, temp DB and output dir."""
    import shorts_digest.digest as digest

    (tmp_path / "settings.yaml").write_text(settings_text())
    (tmp_path / "channels.yaml").write_text(
        f"topics:\n  python:\n    - {{id: {CH_A}, name: A}}\n"
        f"  data_engineering:\n    - {{id: {CH_B}, name: B}}\n"
    )
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(cli, "load_config", lambda: load_config(tmp_path, tmp_path / "none.env"))
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", tmp_path / "cli.db")
    monkeypatch.setattr(digest, "DEFAULT_OUTPUT_DIR", tmp_path / "output")
    # No transcripts in these tests: every Short falls back to title + description.
    from youtube_transcript_api import TranscriptsDisabled

    class NoTranscripts:
        def list(self, video_id):
            raise TranscriptsDisabled(video_id)

    monkeypatch.setattr(transcripts, "YouTubeTranscriptApi", NoTranscripts)
    monkeypatch.setattr("shorts_digest.pipeline.TRANSCRIPT_INTERVAL", 0)

    yt = FakeYouTube(
        channels={CH_A: "UUa", CH_B: "UUb"},
        playlists={"UUa": [], "UUb": []},
    )
    monkeypatch.setattr(youtube, "build_client", lambda key: yt)
    return yt


def last_run():
    conn = store.connect()
    return conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()


def test_run_writes_one_summary_line(cli_env, caplog):
    caplog.set_level("INFO")
    assert cli.main(["run", "--no-discovery"]) == 0
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Run ")]
    assert len(lines) == 1
    assert "success: found=0 kept=0 text=0" in lines[0]
    assert "accepted=0 rejected=0 digest=0" in lines[0]
    assert "today 3/6000" in lines[0]  # 1 channels.list + 2 playlistItems.list
    row = last_run()
    assert (row["status"], row["quota_estimate"]) == ("success", 3)
    assert '"digest": 0' in row["summary"]


def test_crash_in_a_later_stage_is_recorded(cli_env, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("extractor exploded")

    monkeypatch.setattr("shorts_digest.pipeline.extract_stage", boom)
    assert cli.main(["run", "--no-discovery"]) == 1
    row = last_run()
    assert (row["status"], row["stage"]) == ("failed", "extract")
    assert row["error"] == "RuntimeError: extractor exploded"
    assert "stopped in extract stage" in caplog.text


def test_quota_limit_fails_the_run_cleanly(cli_env, monkeypatch, tmp_path):
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        settings.read_text()
        .replace("daily_limit: 6000", "daily_limit: 2")
        .replace("curated_reserve: 500", "curated_reserve: 0")
    )
    assert cli.main(["fetch"]) == 1
    row = last_run()
    assert (row["status"], row["stage"]) == ("failed", "fetch")
    assert row["error"].startswith("QuotaLimitError")
    assert row["quota_estimate"] == 2


def test_interrupted_run_is_closed_by_the_next_one(cli_env, caplog):
    conn = store.connect()
    stuck = store.start_run(conn, "run")  # e.g. the CI job was cancelled mid-run
    assert cli.main(["fetch"]) == 0
    row = conn.execute("SELECT status, error FROM runs WHERE run_id = ?", (stuck,)).fetchone()
    assert tuple(row) == ("failed", "interrupted")
    assert "interrupted" in caplog.text


def test_resume_after_a_crash_finishes_the_leftover_videos(cli_env, monkeypatch):
    """Run 1 dies in the text stage after fetching; run 2 picks the Shorts up."""
    cli_env.playlists_data["UUa"] = [
        {"videoId": "s1", "videoPublishedAt": "2099-01-01T00:00:00Z"},
        {"videoId": "s2", "videoPublishedAt": "2099-01-01T00:00:00Z"},
    ]
    for v in ("s1", "s2"):
        cli_env.videos_data[v] = {
            "channelId": CH_A,
            "duration": "PT40S",
            "publishedAt": "2099-01-01T00:00:00Z",
            "title": "How to use pandas groupby",
            "description": "Use groupby then agg to summarise a DataFrame by column.",
        }

    real_text_stage = __import__("shorts_digest.pipeline", fromlist=["x"]).text_stage
    monkeypatch.setattr(
        "shorts_digest.pipeline.text_stage",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("wifi dropped")),
    )
    assert cli.main(["run", "--no-discovery"]) == 1
    statuses = dict(store.connect().execute("SELECT video_id, status FROM videos").fetchall())
    assert statuses == {"s1": "new", "s2": "new"}  # fetched and saved before the crash

    monkeypatch.setattr("shorts_digest.pipeline.text_stage", real_text_stage)
    assert cli.main(["run", "--no-discovery"]) == 0
    statuses = dict(store.connect().execute("SELECT video_id, status FROM videos").fetchall())
    assert set(statuses.values()) <= {"accepted", "rejected"}  # both made it to the end
    assert last_run()["status"] == "success"


def test_text_stage_resumes_where_it_stopped(conn):
    from shorts_digest.pipeline import text_stage

    store.upsert_channel(conn, CH_A, "A", "python")
    store.insert_videos(
        conn,
        [
            store.VideoRow(f"v{i}", CH_A, f"t{i}", "", f"2026-11-1{i}T00:00:00Z", 30, True)
            for i in range(3)
        ],
    )

    class Disabled:
        def list(self, video_id):
            from youtube_transcript_api import TranscriptsDisabled

            raise TranscriptsDisabled(video_id)

    assert text_stage(conn, limit=1, api=Disabled(), interval=0).processed == 1  # "crash" here
    assert text_stage(conn, api=Disabled(), interval=0).processed == 2  # only the rest
    assert text_stage(conn, api=Disabled(), interval=0).processed == 0


def test_dry_run_makes_no_calls_and_writes_nothing(cli_env, capsys):
    assert cli.main(["run", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "channels:        2" in out
    assert cli_env.calls == []
    assert store.connect().execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_quota_command_shows_breakdown(cli_env, capsys):
    cli.main(["fetch"])
    capsys.readouterr()
    assert cli.main(["quota"]) == 0
    out = capsys.readouterr().out
    assert "3 of 6000 units used" in out
    assert "playlistItems.list" in out
    # Discovery keeps back the 497 reserve units curated hasn't used: 6000 - 497 - 3.
    assert "Remaining: curated 5997, discovery 5500" in out
