"""Phase 9: style/takeaway rules, no_takeaway filter, promotion, New voices, channel commands.

Texts and channels here are made up for the tests.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fakes import CH_A, FakeYouTube

from shorts_digest import store
from shorts_digest.channels import approve_channel, review_lines, set_owner_status
from shorts_digest.config import ChannelsConfig, ConfigError, load_config
from shorts_digest.digest import render_markdown, write_outputs
from shorts_digest.extract import VideoText, extract, load_vocabulary
from shorts_digest.pipeline import build_digest, filter_stage

TOPICS = ["python", "web_dev", "data_engineering", "ai_llm", "cloud_devops", "productivity"]
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
REAL_CONFIG = Path(__file__).resolve().parents[1] / "config"
SMALL = "UC" + "s" * 22
TINY = "UC" + "t" * 22


@pytest.fixture(scope="module")
def vocab():
    return load_vocabulary()


def text(transcript, title, description="", source="transcript"):
    return VideoText("vid", title, description, transcript, source, "web_dev")


# --- style and takeaway ------------------------------------------------------------

JOKE = (
    "Me fixing one bug at five pm on a Friday. Everything works. I push it. "
    "Suddenly forty new bugs appear and the whole team is looking at me. Great weekend."
)
FUNNY_TUTORIAL = (
    "This git trick is hilarious but it saves you. Here's how. Run git stash to park your "
    "changes, then use git switch to move to main, and then git stash pop to bring the work "
    "back. Use this instead of committing half finished code."
)


def test_joke_without_a_lesson_has_no_takeaway(vocab):
    r = extract(text(JOKE, "POV: you fixed one bug 😂", "#programmerhumor"), vocab, TOPICS)
    assert r.style == "skit"
    assert r.has_takeaway is False and r.usefulness <= 2
    assert r.concept.lower().startswith("bug: ")  # the thing being joked about comes first
    assert "no takeaway" in r.reasons


def test_funny_short_with_a_real_technique_keeps_its_score(vocab):
    r = extract(text(FUNNY_TUTORIAL, "This git trick is so funny 😂"), vocab, TOPICS)
    assert r.style == "funny"
    assert r.has_takeaway is True and r.usefulness >= 3


def test_serious_short_is_serious(vocab):
    t = "Use git stash to park changes, then git switch main, then git stash pop afterwards."
    r = extract(text(t * 2, "Git stash in 30 seconds"), vocab, TOPICS)
    assert r.style == "serious" and r.has_takeaway


def test_humor_inside_glued_hashtags(vocab):
    r = extract(text(None, "Like WHATT #boss #codinghumour", source="metadata"), vocab, TOPICS)
    assert r.style == "funny"
    r = extract(text(None, "Monday standup #devpov", source="metadata"), vocab, TOPICS)
    assert r.style == "skit"
    r = extract(text(None, "Git stash #lollipop", source="metadata"), vocab, TOPICS)
    assert r.style == "serious"  # short stems like "lol" are not matched inside hashtags


def test_one_passing_lol_in_speech_is_not_funny(vocab):
    t = FUNNY_TUTORIAL.replace("hilarious", "useful") + " lol."
    assert extract(text(t, "Git stash workflow"), vocab, TOPICS).style == "serious"


# --- filter + promotion ------------------------------------------------------------


def seed(conn, vid, channel, usefulness=4, has_takeaway=True, source="discovery", topic="python"):
    if source == "discovery":
        store.add_discovered_video(
            conn, vid, channel, "Small", topic, f"Title {vid}", "", "2026-11-11T00:00:00Z", 40
        )
    else:
        store.upsert_channel(conn, channel, "Curated", topic)
        store.insert_videos(
            conn, [store.VideoRow(vid, channel, f"T {vid}", "", "2026-11-11T00:00:00Z", 40, True)]
        )
    store.save_text(conn, vid, "metadata", None, None, None)
    store.save_extraction(
        conn,
        vid,
        dict(
            # Distinct per video, or the concept-dedup (rightly) rejects them as repeats.
            concept=f"{vid * 3} technique",
            method_or_tool=None,
            topic=topic,
            summary=f"Summary {vid}.",
            difficulty="beginner",
            usefulness=usefulness,
            is_promotional=False,
            confidence="medium",
            next_step=None,
            style="skit" if not has_takeaway else "funny",
            has_takeaway=has_takeaway,
            reasons=[],
        ),
        "rules",
        "test",
    )


def discovered(conn, channel, subs=12_000, status="candidate"):
    store.upsert_discovered_channel(conn, channel, f"Chan {channel[2]}", "@small", subs, 40)
    if status != "candidate":
        store.set_channel_status(conn, channel, status)


@pytest.fixture
def conn(tmp_path):
    return store.connect(tmp_path / "nv.db")


def test_no_takeaway_is_rejected_with_its_own_reason(conn):
    seed(conn, "joke", SMALL, usefulness=2, has_takeaway=False)
    r = filter_stage(conn, 3, now=NOW)
    assert r.rejected == {"no_takeaway": 1}


def test_channel_suggested_after_three_useful_shorts(conn):
    discovered(conn, SMALL)
    for i in range(2):
        seed(conn, f"s{i}", SMALL)
    seed(conn, "weak", SMALL, usefulness=2)
    assert filter_stage(conn, 3, now=NOW).suggested == []  # only 2 useful so far
    seed(conn, "s2", SMALL)
    assert filter_stage(conn, 3, now=NOW).suggested == [SMALL]
    assert store.get_discovered_channel(conn, SMALL)["status"] == "suggested"


def test_rejected_channel_is_never_suggested_again(conn):
    discovered(conn, SMALL, status="rejected")
    for i in range(4):
        seed(conn, f"s{i}", SMALL)
    assert filter_stage(conn, 3, now=NOW).suggested == []


# --- digest sections ---------------------------------------------------------------


def test_new_voices_and_suggestions_in_the_digest(conn, tmp_path):
    seed(conn, "c1", CH_A, source="curated")
    discovered(conn, SMALL)
    discovered(conn, TINY)
    for i in range(3):
        seed(conn, f"s{i}", SMALL, usefulness=5 - i)
    seed(conn, "t1", TINY, usefulness=3)
    store.save_candidates(
        conn,
        [
            dict(
                video_id="s0",
                channel_id=SMALL,
                keyword="coding joke",
                search_order="date",
                title="x",
                published_at="2026-11-11T00:00:00Z",
                duration_seconds=40,
                views=48_000,
                subscribers=12_000,
                outlier_score=4.0,
                drop_reason=None,
                found_at="2026-11-12T00:00:00+00:00",
                description="",
                language="en",
            )
        ],
    )
    filter_stage(conn, 3, now=NOW)  # accepts all, suggests SMALL
    d = build_digest(conn, 8, 3, 7, exclude_digested=True, now=NOW)

    assert [i.video_id for i in d.items] == ["c1"]  # discovery never takes main slots
    assert [i.video_id for i in d.new_voices] == ["s0", "t1"]  # one per channel, best first
    assert [i.rank for i in d.new_voices] == [2, 3]
    assert [sg.channel_id for sg in d.suggestions] == [SMALL]

    md = render_markdown(d)
    assert "## New voices" in md and "## Channels to consider adding" in md
    assert "2. **s0s0s0 technique** [funny]" in md
    assert '12,000 subscribers · 48,000 views · found by "coding joke"' in md
    assert f"channels approve {SMALL} --topic python" in md

    md_path, csv_path = write_outputs(d, tmp_path)
    rows = csv_path.read_text().splitlines()
    assert rows[0].startswith("rank,section,")
    assert rows[2].startswith("2,discovery,")


def test_suggestion_shown_at_most_weekly_in_runs(conn):
    discovered(conn, SMALL, status="suggested")
    store.mark_suggested_shown(conn, [SMALL])
    assert build_digest(conn, 8, 3, 7, exclude_digested=True, now=NOW).suggestions == []
    later = datetime.now(UTC) + timedelta(days=8)
    assert len(build_digest(conn, 8, 3, 7, exclude_digested=True, now=later).suggestions) == 1
    # A rebuild (digest --days N) always lists pending suggestions.
    assert len(build_digest(conn, 8, 3, 7, exclude_digested=False, now=NOW).suggestions) == 1


def test_empty_sections_are_left_out(conn):
    seed(conn, "c1", CH_A, source="curated")
    filter_stage(conn, 3, now=NOW)
    md = render_markdown(build_digest(conn, 8, 3, 7, exclude_digested=True, now=NOW))
    assert "New voices" not in md and "Channels to consider" not in md


# --- channel commands --------------------------------------------------------------

YAML = f"topics:\n  python:\n    - {{id: {CH_A}, name: A}}\n  web_dev: []\n"


def test_review_lists_commands(conn):
    discovered(conn, SMALL, status="suggested")
    seed(conn, "s0", SMALL)
    filter_stage(conn, 3, now=NOW)
    text_out = "\n".join(review_lines(store.channel_suggestions(conn)))
    assert f"channels approve {SMALL} --topic python" in text_out
    assert "useful Shorts: 1" in text_out


def test_approve_writes_after_diff_and_makes_channel_curated(conn, tmp_path):
    path = tmp_path / "channels.yaml"
    path.write_text(YAML)
    discovered(conn, SMALL, status="suggested")
    seed(conn, "s0", SMALL)
    out: list[str] = []
    assert approve_channel(conn, path, SMALL, "web_dev", TOPICS, lambda p: "y", out.append)
    saved = ChannelsConfig.model_validate(yaml.safe_load(path.read_text()))
    assert [c.id for c in saved.topics["web_dev"]] == [SMALL]
    assert any(line.startswith("+    - {id: " + SMALL) for line in "\n".join(out).splitlines())
    assert store.get_discovered_channel(conn, SMALL)["status"] == "approved"
    row = conn.execute("SELECT source, topic FROM channels WHERE channel_id = ?", (SMALL,))
    assert tuple(row.fetchone()) == ("curated", "web_dev")


def test_approve_declined_changes_nothing(conn, tmp_path):
    path = tmp_path / "channels.yaml"
    path.write_text(YAML)
    discovered(conn, SMALL, status="suggested")
    assert not approve_channel(conn, path, SMALL, "web_dev", TOPICS, lambda p: "n", print)
    assert path.read_text() == YAML
    assert store.get_discovered_channel(conn, SMALL)["status"] == "suggested"


def test_approve_unknown_channel_or_topic(conn, tmp_path):
    path = tmp_path / "channels.yaml"
    path.write_text(YAML)
    with pytest.raises(ConfigError, match="not a discovered channel"):
        approve_channel(conn, path, SMALL, "web_dev", TOPICS, lambda p: "y", print)
    discovered(conn, SMALL)
    with pytest.raises(ConfigError, match="not one of"):
        approve_channel(conn, path, SMALL, "gaming", TOPICS, lambda p: "y", print)


def test_block_withdraws_unsent_shorts(conn):
    discovered(conn, SMALL)
    seed(conn, "s0", SMALL)
    filter_stage(conn, 3, now=NOW)
    out: list[str] = []
    set_owner_status(conn, SMALL, "blocked", out.append)
    assert "1 pending Short(s) withdrawn" in out[0]
    row = conn.execute("SELECT status, reject_reason FROM videos WHERE video_id = 's0'")
    assert tuple(row.fetchone()) == ("rejected", "channel_blocked")


# --- run end to end ----------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    from youtube_transcript_api import TranscriptsDisabled

    import shorts_digest.cli as cli
    import shorts_digest.digest as digest
    import shorts_digest.transcripts as transcripts
    import shorts_digest.youtube as youtube

    (tmp_path / "settings.yaml").write_text((REAL_CONFIG / "settings.yaml").read_text())
    (tmp_path / "channels.yaml").write_text(f"topics:\n  python:\n    - {{id: {CH_A}, name: A}}\n")
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake-key-for-tests")
    monkeypatch.setattr(cli, "load_config", lambda: load_config(tmp_path, tmp_path / "none.env"))
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", tmp_path / "run.db")
    monkeypatch.setattr(digest, "DEFAULT_OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr("shorts_digest.pipeline.TRANSCRIPT_INTERVAL", 0)

    class NoTranscripts:
        def list(self, video_id):
            raise TranscriptsDisabled(video_id)

    monkeypatch.setattr(transcripts, "YouTubeTranscriptApi", NoTranscripts)
    published = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    yt = FakeYouTube(
        channels={CH_A: "UUa"},
        playlists={"UUa": []},
        info={SMALL: {"title": "Small Dev", "subs": 10_000, "videos": 40}},
        videos={
            "nv1": {
                "channelId": SMALL,
                "views": 80_000,
                "duration": "PT40S",
                "publishedAt": published,
                "title": "How to use git stash instead of committing broken code",
                "description": "Here's how: run git stash, then git switch main, then use "
                "git stash pop to bring your changes back. Use this instead of a messy "
                "commit. You can also run git stash list to see everything you parked, "
                "and git stash drop to delete one you no longer need.",
            }
        },
        video_search={"python trick": ["nv1"]},
    )
    monkeypatch.setattr(youtube, "build_client", lambda key: yt)
    return cli, tmp_path


def test_run_with_discovery_reaches_new_voices(cli_env, caplog):
    cli, tmp_path = cli_env
    caplog.set_level("INFO")
    assert cli.main(["run"]) == 0
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Run "))
    assert "discovered=1 (searches 13)" in line
    assert "(+1 new voices)" in line and "discovery 13" in line
    md = next((tmp_path / "output").glob("digest-*.md")).read_text()
    assert "## New voices" in md and "git stash" in md.lower()


def test_discovery_failure_never_stops_the_curated_run(cli_env, monkeypatch, caplog):
    cli, _ = cli_env
    caplog.set_level("INFO")

    def boom(*a, **k):
        raise RuntimeError("search exploded")

    monkeypatch.setattr("shorts_digest.discovery.discover", boom)
    assert cli.main(["run"]) == 0
    assert "discovery=FAILED" in caplog.text
    row = store.connect().execute("SELECT status, summary FROM runs").fetchone()
    assert row["status"] == "success" and "search exploded" in row["summary"]


def test_no_discovery_flag(cli_env, caplog):
    cli, _ = cli_env
    caplog.set_level("INFO")
    assert cli.main(["run", "--no-discovery"]) == 0
    assert "discovered=" not in caplog.text
