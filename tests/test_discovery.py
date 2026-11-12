"""Discovery: keyword rotation, filters with reasons, reposts, ranking caps, dry run."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import CH_A, FakeYouTube, make_guard

from shorts_digest import store
from shorts_digest.config import DiscoveryConfig, load_config
from shorts_digest.discovery import (
    POSITION_KEY,
    Candidate,
    check_candidate,
    discover,
    language_ok,
    mark_reposts,
    order_for,
    rank_and_cap,
    render_report,
    same_title,
    todays_keywords,
    write_report_csv,
)

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
REAL_CONFIG = Path(__file__).resolve().parents[1] / "config"


def ch(c):  # fake channel IDs with the right shape
    return "UC" + c * 22


SMALL, BIG, TINY, HIDDEN, NEWBIE = ch("s"), ch("g"), ch("t"), ch("h"), ch("n")


# --- pure helpers ------------------------------------------------------------------


def test_keyword_rotation_wraps_and_never_repeats():
    kws = ["a", "b", "c", "d", "e"]
    assert todays_keywords(kws, 3, 3) == ["d", "e", "a"]
    assert todays_keywords(kws, 0, 15) == kws  # cap larger than the list: each once


def test_order_alternates_and_flips_daily():
    day1 = [order_for(i, "2026-11-12") for i in range(4)]
    day2 = [order_for(i, "2026-11-13") for i in range(4)]
    assert day1[0] != day1[1] and day1[0] == day1[2]
    assert day1[0] != day2[0]  # the same keyword gets the other order tomorrow


def cand(**kw):
    base = dict(
        video_id="v",
        channel_id=SMALL,
        title="t",
        published_at="2026-11-11T00:00:00Z",
        keyword="k",
        search_order="viewCount",
        duration_seconds=40,
        views=50_000,
        subscribers=10_000,
        video_count=50,
        outlier_score=5.0,
    )
    return Candidate(**(base | kw))


D = DiscoveryConfig()  # defaults = the spec's thresholds


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"duration_seconds": None}, "video_unavailable"),
        ({"duration_seconds": 400}, "not_short"),
        ({"channel_id": CH_A}, "curated_channel"),
        ({"channel_id": ch("r")}, "channel_rejected"),
        ({"channel_id": ch("u")}, "channel_unavailable"),
        ({"subscribers": None}, "subscribers_hidden"),
        ({"subscribers": 999}, "too_few_subscribers"),
        ({"subscribers": 100_001}, "too_many_subscribers"),
        ({"video_count": 4}, "too_few_uploads"),
        ({"views": 4_999}, "too_few_views"),
        ({"outlier_score": 1.9}, "low_views_per_subscriber"),
        ({}, None),
    ],
)
def test_each_filter_has_its_reason(overrides, reason):
    known = {SMALL, CH_A, ch("r")}
    status = {ch("r"): "rejected"}
    assert check_candidate(cand(**overrides), D, 180, {CH_A}, status, known) == reason


def test_boundaries_are_inclusive():
    c = cand(subscribers=1000, views=5000, outlier_score=2.0, video_count=5)
    assert check_candidate(c, D, 180, set(), {}, {SMALL}) is None


def test_same_title_needs_enough_words():
    assert same_title("This Python trick saves hours!!", "this python trick saves hours")
    assert not same_title("Python tip", "Python tip")  # too short to call it a repost
    assert not same_title("This Python trick saves hours", "This SQL trick saves money")


def test_repost_keeps_the_earliest_copy():
    original = cand(
        video_id="o",
        channel_id=BIG,
        published_at="2026-11-10T00:00:00Z",
        title="The one git command you need",
        drop_reason="too_many_subscribers",
    )
    repost = cand(
        video_id="r",
        channel_id=SMALL,
        published_at="2026-11-11T00:00:00Z",
        title="The one Git command you need!",
    )
    other = cand(video_id="x", channel_id=TINY, title="Totally different video about SQL joins")
    mark_reposts([repost, original, other], stored=[])
    assert repost.drop_reason == "likely_repost"  # the big channel had it first
    assert original.drop_reason == "too_many_subscribers"  # unchanged
    assert other.kept


def test_repost_of_a_stored_video_but_not_of_a_later_one():
    c = cand(title="Five Excel shortcuts you must know", published_at="2026-11-11T00:00:00Z")
    mark_reposts([c], stored=[("Five Excel shortcuts you must know", CH_A, "2026-11-12T00:00Z")])
    assert c.kept  # the stored copy is *later*, so this one is the original
    mark_reposts([c], stored=[("Five Excel shortcuts you must know", CH_A, "2026-11-01T00:00Z")])
    assert c.drop_reason == "likely_repost"


def test_rank_one_per_channel_and_daily_cap():
    a1 = cand(video_id="a1", channel_id=SMALL, outlier_score=9)
    a2 = cand(video_id="a2", channel_id=SMALL, outlier_score=8)
    b = cand(video_id="b", channel_id=TINY, outlier_score=7)
    c = cand(video_id="c", channel_id=NEWBIE, outlier_score=6)
    d = cand(video_id="d", channel_id=HIDDEN, outlier_score=5)
    rank_and_cap([d, c, b, a2, a1], channels_kept_today={NEWBIE}, kept_today=1, cap=3)
    assert [x.drop_reason for x in (a1, a2, b, c, d)] == [
        None,
        "channel_daily_cap",
        None,
        "channel_daily_cap",
        "over_daily_cap",
    ]


# --- the job against a fake API ----------------------------------------------------


@pytest.fixture
def cfg(tmp_path):
    settings = (REAL_CONFIG / "settings.yaml").read_text()
    (tmp_path / "settings.yaml").write_text(settings)
    (tmp_path / "channels.yaml").write_text(f"topics:\n  python:\n    - {{id: {CH_A}, name: A}}\n")
    return load_config(tmp_path, env_file=tmp_path / "none.env")


@pytest.fixture
def conn(tmp_path):
    return store.connect(tmp_path / "d.db")


def v(channel, views, duration="PT40S", title=None, ts="2026-11-11T10:00:00Z"):
    return {
        "channelId": channel,
        "views": views,
        "duration": duration,
        "publishedAt": ts,
        "title": title or f"Python trick from {channel[2]} with {views} views",
    }


def make_yt():
    return FakeYouTube(
        videos={
            "keep1": v(SMALL, 90_000),  # 9x its subscribers
            "keep2": v(TINY, 30_000),  # 10x
            "same_channel": v(SMALL, 50_000),  # second video from SMALL: one per day
            "big": v(BIG, 900_000),
            "hidden": v(HIDDEN, 50_000),
            "newbie": v(NEWBIE, 50_000),
            "long": v(SMALL, 90_000, duration="PT3M30S"),
            "curated": v(CH_A, 90_000),
            "weak": v(TINY, 6_000),  # 2.0x? no: 6,000 / 3,000 = 2.0 -> kept by ratio
        },
        info={
            SMALL: {"title": "Small", "subs": 10_000, "videos": 40},
            TINY: {"title": "Tiny", "subs": 3_000, "videos": 12},
            BIG: {"title": "Big", "subs": 2_000_000, "videos": 900},
            HIDDEN: {"title": "Hidden", "hidden": True, "videos": 30},
            NEWBIE: {"title": "Newbie", "subs": 5_000, "videos": 2},
        },
        video_search={
            "python trick": ["keep1", "big", "long", "curated", "same_channel"],
            "python tips": ["keep2", "hidden", "newbie", "keep1", "weak"],  # keep1 again
        },
    )


def run(cfg, conn, yt, **kw):
    return discover(cfg, conn, yt, make_guard(conn, source="discovery"), now=NOW, **kw)


def test_full_job_reasons_and_storage(cfg, conn):
    yt = make_yt()
    r = run(cfg, conn, yt, max_searches=2)
    assert r.keywords == ["python trick", "python tips"] and r.searches == 2
    reasons = {c.video_id: c.drop_reason for c in r.candidates}
    assert reasons == {
        "keep1": None,
        "keep2": None,
        "weak": "channel_daily_cap",  # TINY already has keep2 (higher outlier)
        "same_channel": "channel_daily_cap",
        "big": "too_many_subscribers",
        "hidden": "subscribers_hidden",
        "newbie": "too_few_uploads",
        "long": "not_short",
        "curated": "curated_channel",
    }
    assert [c.video_id for c in r.kept] == ["keep2", "keep1"]  # x10 before x9
    assert next(c for c in r.candidates if c.video_id == "keep1").keyword == "python trick"
    # Search params follow the spec.
    params = [kw for name, kw in yt.calls if name == "search.list"][0]
    expected = {
        "type": "video",
        "videoDuration": "short",
        "maxResults": 50,
        "safeSearch": "moderate",
        "relevanceLanguage": "en",
        "publishedAfter": "2026-11-05T12:00:00Z",
    }
    assert expected.items() <= params.items()
    # Everything stored, dropped ones with reasons; rotation advanced; searches logged.
    rows = dict(conn.execute("SELECT video_id, drop_reason FROM discovery_candidates").fetchall())
    assert rows == reasons
    assert store.get_state(conn, POSITION_KEY) == "2"
    assert conn.execute("SELECT count(*) FROM discovery_searches").fetchone()[0] == 2
    # No stats were fetched for the curated channel.
    looked_up = ",".join(kw["id"] for name, kw in yt.calls if name == "channels.list")
    assert CH_A not in looked_up


def test_second_run_skips_judged_videos_and_uses_cached_stats(cfg, conn):
    run(cfg, conn, make_yt(), max_searches=1)
    store.set_state(conn, POSITION_KEY, "0")  # search the same keyword again
    yt = make_yt()
    yt.videos_data["fresh"] = v(SMALL, 70_000, title="A brand new Short from Small")
    yt.video_search["python trick"].append("fresh")
    r = run(cfg, conn, yt, max_searches=1)
    assert [c.video_id for c in r.candidates] == ["fresh"]  # the other 5 were judged already
    assert r.already_seen == 5
    assert yt.count("channels.list") == 0  # SMALL's stats came from the 7-day cache
    assert r.candidates[0].subscribers == 10_000


def test_dry_run_saves_nothing_but_logs_the_spent_searches(cfg, conn):
    r = run(cfg, conn, make_yt(), dry_run=True, max_searches=2)
    assert len(r.kept) == 2
    assert conn.execute("SELECT count(*) FROM discovery_candidates").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM discovered_channels").fetchone()[0] == 0
    assert store.get_state(conn, POSITION_KEY) is None
    # The searches really happened (real quota), so they count toward today's cap.
    assert store.searches_on(conn, make_guard(conn).day) == 2


def test_daily_search_cap_is_shared_across_invocations(cfg, conn):
    run(cfg, conn, make_yt(), dry_run=True, max_searches=10)
    r = run(cfg, conn, make_yt())  # 15 per day; 10 used (12 keywords -> 10 + 5 left)
    assert r.searches == 5
    r = run(cfg, conn, make_yt())
    assert r.searches == 0 and "daily search cap" in r.stopped


def test_quota_guard_stops_searching_but_keeps_results(cfg, conn):
    guard = make_guard(conn, source="discovery", daily_limit=650, reserve=500)  # 150 for us
    r = discover(cfg, conn, make_yt(), guard, now=NOW)
    assert r.searches == 1 and "refused" in r.stopped
    assert {c.video_id for c in r.candidates} >= {"keep1"}  # still judged


def test_blocked_channel_dropped(cfg, conn):
    store.upsert_discovered_channel(conn, SMALL, "Small", None, 10_000, 40)
    with conn:
        conn.execute("UPDATE discovered_channels SET status = 'blocked'")
    r = run(cfg, conn, make_yt(), max_searches=1)
    assert {c.video_id: c.drop_reason for c in r.candidates}["keep1"] == "channel_blocked"


# --- report ------------------------------------------------------------------------


def test_report_and_csv(cfg, conn, tmp_path):
    r = run(cfg, conn, make_yt(), dry_run=True, max_searches=2)
    text = render_report(r, show_dropped=1)
    assert "DRY RUN" in text and "KEPT (2)" in text and "x10.0" in text
    assert "too_many_subscribers: 1" in text
    path = write_report_csv(r, tmp_path / "out", "2026-11-12")
    assert path.name == "discovery-2026-11-12-dryrun.csv"
    lines = path.read_text().splitlines()
    assert lines[0].startswith("kept,drop_reason,outlier_score")
    assert lines[1].startswith("True,,10.0") and len(lines) == 1 + len(r.candidates)


def test_settings_validation():
    with pytest.raises(ValueError):
        DiscoveryConfig(max_searches_per_day=16)
    with pytest.raises(ValueError):
        DiscoveryConfig(enabled=True, keywords=[])
    with pytest.raises(ValueError):
        DiscoveryConfig(keywords=["a", "A"])


def test_cli_discover_dry_run_prints_report_and_csv(cfg, monkeypatch, tmp_path, capsys):
    import shorts_digest.cli as cli
    import shorts_digest.digest as digest
    import shorts_digest.youtube as youtube

    monkeypatch.setenv("YOUTUBE_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(cli, "load_config", lambda: load_config(tmp_path, tmp_path / "none.env"))
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", tmp_path / "cli.db")
    monkeypatch.setattr(digest, "DEFAULT_OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(youtube, "build_client", lambda key: make_yt())
    assert cli.main(["discover", "--dry-run", "--max-searches", "2"]) == 0
    assert "KEPT (2)" in capsys.readouterr().out
    assert [p.name for p in (tmp_path / "output").iterdir()][0].endswith("-dryrun.csv")


# --- topic and language filters ----------------------------------------------------


@pytest.mark.parametrize(
    "language, title, ok",
    [
        ("en", "anything", True),
        ("en-US", "anything", True),
        ("hi", "anything", True),
        ("pl", "dress code marzę", False),  # tagged Polish
        ("es-419", "Una IA hackea Linux", False),
        ("zxx", "music only", True),
        (None, "Python trick 🔥 #shorts", True),
        (None, "Python seekho आसान तरीके से", True),  # Hinglish + Devanagari
        (None, "Excel फॉर्मूला", True),
        (None, "Pythonのコツ", False),  # Japanese script
        (None, "Трюк в Excel", False),  # Cyrillic
    ],
)
def test_language_ok(language, title, ok):
    assert language_ok(language, title, ["en", "hi"]) is ok


def test_off_topic_uses_terms_and_tech_words():
    from shorts_digest.extract import load_vocabulary

    vocab = load_vocabulary()
    known = {SMALL}

    def reason(title, description=""):
        c = cand(title=title, description=description)
        return check_candidate(c, D, 180, set(), {}, known, vocab)

    assert reason("NEW FUNNY CRAFTLAND MAP IN FREE FIRE || CODE PIN COMMENT") == "off_topic"
    assert reason("Nobody knows the dress code!") == "off_topic"
    assert reason("He turned into a big python on AGT") is None  # "python" is a term
    assert reason("Why fix the bug when you can simply HIDE it?") is None  # tech word
    assert reason("Wait for it 😂", description="#coding #programmerlife") is None
    assert reason("Ctrl+Z won't save you", description="") == "off_topic"  # needs a word
    assert reason("Like WHATT #boss #worklife #vibecoding #codinghumour") is None  # stems
    assert reason("Happy day #happy #devotional #daily") == "off_topic"  # no false stems


def test_job_drops_off_topic_and_foreign_language(cfg, conn):
    yt = make_yt()
    yt.videos_data["freefire"] = v(SMALL, 90_000, title="Craftland map code pin 🔥")
    yt.videos_data["polish"] = v(TINY, 90_000, title="Python sztuczka")
    yt.videos_data["polish"]["defaultAudioLanguage"] = "pl"
    yt.video_search["python trick"] = ["freefire", "polish"]
    r = run(cfg, conn, yt, max_searches=1)
    assert {c.video_id: c.drop_reason for c in r.candidates} == {
        "freefire": "off_topic",
        "polish": "wrong_language",
    }
