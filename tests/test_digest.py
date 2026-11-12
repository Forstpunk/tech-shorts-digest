"""Filter stage, selection and rendering. Items here are made up for the tests."""

import csv
from datetime import UTC, date, datetime

from shorts_digest import store
from shorts_digest.digest import Digest, DigestItem, render_markdown, select, write_outputs
from shorts_digest.pipeline import build_digest, filter_stage

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
CH = "UC" + "a" * 22


def item(
    vid, topic="python", usefulness=4, confidence="high", published="2026-11-11T00:00:00Z", **kw
):
    base = dict(
        video_id=vid,
        topic=topic,
        concept=f"Concept {vid}",
        summary=f"Summary {vid}.",
        method_or_tool="pandas",
        difficulty="beginner",
        usefulness=usefulness,
        confidence=confidence,
        next_step='Search: "pandas tutorial"',
        url=f"https://www.youtube.com/shorts/{vid}",
        channel="Chan",
        published_at=published,
        text_source="transcript",
    )
    return DigestItem(**(base | kw))


# --- selection ---------------------------------------------------------------------


def test_select_ranks_by_usefulness_then_confidence():
    items = [
        item("a", usefulness=3),
        item("b", usefulness=5),
        item("c", usefulness=5, confidence="low"),
    ]
    assert [i.video_id for i in select(items, 10, 10)] == ["b", "c", "a"]


def test_select_caps_per_topic_and_total():
    items = [item(f"ai{i}", "ai_llm", 5) for i in range(5)] + [item("py", "python", 3)]
    picked = select(items, size=4, max_per_topic=3)
    assert [i.topic for i in picked].count("ai_llm") == 3
    assert "py" in [i.video_id for i in picked] and len(picked) == 4


def test_select_groups_by_topic_and_numbers_in_reading_order():
    items = [item("a1", "ai_llm", 5), item("p1", "python", 4), item("a2", "ai_llm", 3)]
    picked = select(items, 10, 10)
    assert [(i.video_id, i.rank) for i in picked] == [("a1", 1), ("a2", 2), ("p1", 3)]


# --- rendering ---------------------------------------------------------------------


def test_markdown_matches_claude_md_format():
    d = Digest(date(2026, 11, 12), select([item("x", "ai_llm")], 8, 3), 41, 22)
    md = render_markdown(d)
    assert md.splitlines()[:4] == [
        "# Tech Shorts Digest — 12 Nov 2026",
        "1 picked from 41 new Shorts across 22 channels.",
        "",
        "## AI / LLM",
    ]
    assert "1. **Concept x** — Summary x." in md
    assert "   Tool/method: pandas · Level: beginner · Score: 4/5" in md
    assert '   Next step: Search: "pandas tutorial"' in md
    assert "   https://www.youtube.com/shorts/x — Chan" in md


def test_markdown_omits_missing_tool_and_next_step():
    d = Digest(
        date(2026, 11, 12), select([item("x", method_or_tool=None, next_step=None)], 8, 3), 1, 1
    )
    md = render_markdown(d)
    assert "Tool/method" not in md and "Next step" not in md
    assert "Level: beginner · Score: 4/5" in md


def test_empty_digest_says_so():
    md = render_markdown(Digest(date(2026, 11, 12), [], 5, 3))
    assert "0 picked from 5 new Shorts" in md and "Nothing passed the filters" in md


def test_write_outputs_md_and_csv(tmp_path):
    d = Digest(date(2026, 11, 12), select([item("x"), item("y", usefulness=5)], 8, 3), 2, 1)
    md_path, csv_path = write_outputs(d, tmp_path)
    assert md_path.name == "digest-2026-11-12.md" and csv_path.name == "digest-2026-11-12.csv"
    rows = list(csv.DictReader(csv_path.open()))
    assert [(r["rank"], r["video_id"]) for r in rows] == [("1", "y"), ("2", "x")]


# --- filter + build from SQLite ----------------------------------------------------


def seed(conn, specs):
    """specs: (video_id, usefulness, is_promotional, concept, topic)"""
    store.upsert_channel(conn, CH, "Chan", "python")
    for vid, useful, promo, concept, topic in specs:
        store.insert_videos(
            conn, [store.VideoRow(vid, CH, concept, "", "2026-11-11T00:00:00Z", 40, True)]
        )
        store.save_text(conn, vid, "transcript", "text", "en", None)
        store.save_extraction(
            conn,
            vid,
            dict(
                concept=concept,
                method_or_tool=None,
                topic=topic,
                summary="S.",
                difficulty="beginner",
                usefulness=useful,
                is_promotional=promo,
                confidence="high",
                next_step=None,
                reasons=[],
            ),
            "rules",
            "test",
        )


def statuses(conn):
    return {
        r[0]: (r[1], r[2])
        for r in conn.execute("SELECT video_id, status, reject_reason FROM videos")
    }


def test_filter_stage_reasons(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    seed(
        conn,
        [
            ("good", 5, False, "5 Excel shortcuts", "productivity"),
            ("dup", 4, False, "5 Excel Shortcuts!!", "productivity"),
            ("low", 2, False, "Robot does backflip", "ai_llm"),
            ("ad", 4, True, "My Python course", "python"),
        ],
    )
    r = filter_stage(conn, min_usefulness=3, now=NOW)
    assert r.accepted == 1
    assert statuses(conn) == {
        "good": ("accepted", None),
        "dup": ("rejected", "duplicate_concept"),  # the better-scored copy was kept
        "low": ("rejected", "low_usefulness"),
        "ad": ("rejected", "promotional"),
    }


def test_build_digest_skips_already_sent_items(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    seed(conn, [("a", 5, False, "Concept A", "python"), ("b", 4, False, "Concept B", "web_dev")])
    filter_stage(conn, 3, now=NOW)
    first = build_digest(conn, 8, 3, days=7, exclude_digested=True, now=NOW)
    assert [i.video_id for i in first.items] == ["a", "b"] and first.shorts_considered == 2
    run_id = store.start_run(conn, "run")
    store.record_digest(conn, run_id, [(i.video_id, i.rank) for i in first.items])
    again = build_digest(conn, 8, 3, days=7, exclude_digested=True, now=NOW)
    assert again.items == [] and again.shorts_considered == 0
    # `digest --days` rebuilds from everything stored, sent or not
    rebuilt = build_digest(conn, 8, 3, days=7, exclude_digested=False, now=NOW)
    assert len(rebuilt.items) == 2


def test_build_digest_respects_window(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    seed(conn, [("a", 5, False, "Concept A", "python")])
    filter_stage(conn, 3, now=NOW)
    later = datetime(2026, 12, 1, tzinfo=UTC)
    assert build_digest(conn, 8, 3, days=7, exclude_digested=False, now=later).items == []


def test_recorded_runs_do_not_overwrite_same_day_digest(tmp_path):

    d = Digest(date(2026, 11, 12), [], 0, 0)
    first, _ = write_outputs(d, tmp_path, overwrite=False)
    second, csv2 = write_outputs(d, tmp_path, overwrite=False)
    rebuilt, _ = write_outputs(d, tmp_path)  # digest --days N rebuilds in place
    assert first.name == "digest-2026-11-12.md"
    assert (second.name, csv2.name) == ("digest-2026-11-12-2.md", "digest-2026-11-12-2.csv")
    assert rebuilt == first
