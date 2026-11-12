"""Rule-based extractor. Texts here are made up for the tests, not real Shorts."""

import pytest

from shorts_digest import store
from shorts_digest.extract import (
    VideoText,
    clean_title,
    extract,
    load_vocabulary,
    split_sentences,
)
from shorts_digest.pipeline import extract_stage

TOPICS = ["python", "web_dev", "data_engineering", "ai_llm", "cloud_devops", "productivity"]


@pytest.fixture(scope="module")
def vocab():
    return load_vocabulary()  # the real config/terms.yaml


def video(transcript, title="A title", topic="python", source="transcript", description=""):
    return VideoText("vid", title, description, transcript, source, topic)


SHORTCUTS = (
    "Here's how to move fast in Excel. Press Ctrl + Shift + L to add a filter. "
    "Then use Alt + equals to autosum the column. Next press control A to select the table, "
    "and you can hit Alt H O I to autofit. Use these instead of the mouse and you will be "
    "faster every single day at work with your formula sheets."
)


def test_specific_tutorial_scores_5(vocab):
    r = extract(video(SHORTCUTS, "5 Excel shortcuts", "productivity"), vocab, TOPICS)
    assert r.usefulness == 5
    assert r.topic == "productivity"
    assert r.method_or_tool.startswith("excel")
    assert r.confidence == "high"
    assert any("specific steps" in reason for reason in r.reasons)


def test_shared_term_goes_to_channel_topic(vocab):
    # "excel" is in both productivity and data_engineering: the channel's topic breaks the tie
    text = "Open Excel and you can see the whole table at once in one place for the team."
    assert (
        extract(video(text, "Excel", "data_engineering"), vocab, TOPICS).topic == "data_engineering"
    )
    assert extract(video(text, "Excel", "productivity"), vocab, TOPICS).topic == "productivity"


def test_hype_without_substance_scores_low(vocab):
    text = (
        "This is the coolest thing you will see today. A robot that does backflips. "
        "It is insane and honestly mind blowing, nobody is talking about this at all right now."
    )
    r = extract(video(text, "This robot is INSANE", "ai_llm"), vocab, TOPICS)
    assert r.usefulness == 1


def test_opinion_is_penalised(vocab):
    text = (
        "Data engineer versus cloud engineer, which salary is higher? It depends on the "
        "company and the city, but both roles use SQL and both are in demand in the market today."
    )
    r = extract(video(text, "Data vs Cloud salary", "cloud_devops"), vocab, TOPICS)
    assert r.usefulness <= 2
    assert any("news/opinion" in reason for reason in r.reasons)


def test_headline_concept_scores_3(vocab):
    text = (
        "A decorator in Python wraps a function. You can use one to add logging or timing "
        "without changing the function itself, which keeps your code clean and easy to read."
    )
    r = extract(video(text, "Python decorators explained"), vocab, TOPICS)
    assert r.usefulness == 3
    assert r.difficulty == "intermediate"
    assert r.next_step == 'Search: "decorator tutorial"'


def test_thin_text_is_capped_with_low_confidence(vocab):
    r = extract(
        video(None, "pandas trick", source="metadata", description="pandas merge tip use it"),
        vocab,
        TOPICS,
    )
    assert r.confidence == "low" and r.usefulness <= 2
    assert any("no transcript" in reason for reason in r.reasons)


def test_promo_detected_from_speech(vocab):
    text = (
        "Before we start, this video is sponsored by a company. Sign up with the link in bio "
        "and use code SAVE for a discount on the course, which teaches python from scratch."
    )
    assert extract(video(text), vocab, TOPICS).is_promotional


def test_description_links_alone_are_not_promo(vocab):
    r = extract(
        video(SHORTCUTS, description="Sign up for my course! Link in bio. Use code X."),
        vocab,
        TOPICS,
    )
    assert not r.is_promotional


def test_advanced_term_sets_difficulty(vocab):
    text = (
        "Python's GIL means only one thread runs bytecode at a time, so use processes for CPU work."
    )
    assert extract(video(text), vocab, TOPICS).difficulty == "advanced"


def test_summary_is_a_real_sentence_from_the_text(vocab):
    r = extract(video(SHORTCUTS, "x", "productivity"), vocab, TOPICS)
    assert r.summary.rstrip("…") in SHORTCUTS


def test_no_false_shortcut_from_prose(vocab):
    text = "You have to shift a lot of your thinking and take control of the command line daily."
    r = extract(video(text), vocab, TOPICS)
    assert r.method_or_tool is None  # no shortcut, no tool


def test_clean_title_strips_hashtags_and_emoji():
    assert clean_title("Use pandas like a pro 🐼🔥 #python #shorts") == "Use pandas like a pro"


def test_split_sentences_handles_unpunctuated_captions():
    text = " ".join(["word"] * 60)
    assert len(split_sentences(text)) == 3


def test_extract_stage_stores_results_and_is_idempotent(tmp_path, vocab):
    conn = store.connect(tmp_path / "t.db")
    ch = "UC" + "a" * 22
    store.upsert_channel(conn, ch, "A", "productivity")
    store.insert_videos(conn, [store.VideoRow("v1", ch, "Excel tips", "", "2026-11-11", 40, True)])
    store.save_text(conn, "v1", "transcript", SHORTCUTS, "en", None)

    r = extract_stage(conn, TOPICS, vocab=vocab)
    assert r.processed == 1 and r.by_score == {5: 1}
    row = conn.execute("SELECT * FROM extractions JOIN videos USING (video_id)").fetchone()
    assert row["status"] == "extracted" and row["model"] == "rules" and "specific" in row["reasons"]
    assert extract_stage(conn, TOPICS, vocab=vocab).processed == 0
