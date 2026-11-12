"""Transcript selection and fallbacks, with a fake caption API (no network)."""

from dataclasses import dataclass

import pytest
from youtube_transcript_api import (
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
)

from shorts_digest import store
from shorts_digest.pipeline import MAX_CONSECUTIVE_BLOCKS, text_stage
from shorts_digest.transcripts import fetch_text, metadata_text

SPEECH = "here is how a python list comprehension replaces a for loop in one line"


@dataclass
class Snip:
    text: str


@dataclass
class Fetched:
    snippets: list


class FakeTranscript:
    def __init__(self, code, generated, text=SPEECH):
        self.language_code, self.is_generated, self._text = code, generated, text

    def fetch(self):
        return Fetched([Snip(w) for w in ["[Music]", *self._text.split(" ")]])


class FakeList:
    def __init__(self, transcripts):
        self._t = transcripts

    def find_transcript(self, codes):
        for code in codes:  # manual first, then generated, like the real library
            for want_generated in (False, True):
                for t in self._t:
                    if t.language_code == code and t.is_generated == want_generated:
                        return t
        raise NoTranscriptFound("vid", codes, None)

    def __iter__(self):
        return iter(sorted(self._t, key=lambda t: t.is_generated))


class FakeApi:
    """per_video: {video_id: [FakeTranscript, ...] or Exception}"""

    def __init__(self, per_video=None, default=None):
        self.per_video, self.default, self.calls = per_video or {}, default, []

    def list(self, video_id):
        self.calls.append(video_id)
        data = self.per_video.get(video_id, self.default)
        if isinstance(data, Exception):
            raise data
        return FakeList(data or [])


def test_prefers_manual_english():
    api = FakeApi({"v": [FakeTranscript("en", True, "auto " * 10), FakeTranscript("en", False)]})
    r = fetch_text("v", api)
    assert r.text_source == "transcript" and r.transcript == SPEECH and r.language == "en"


def test_english_before_hindi():
    api = FakeApi(
        {"v": [FakeTranscript("hi", False, "hindi " * 10), FakeTranscript("en-US", True)]}
    )
    assert fetch_text("v", api).language == "en-US"


def test_falls_back_to_any_generated_track():
    api = FakeApi({"v": [FakeTranscript("es", False, "manual " * 10), FakeTranscript("de", True)]})
    assert fetch_text("v", api).language == "de"


def test_music_tags_are_dropped():
    r = fetch_text("v", FakeApi({"v": [FakeTranscript("en", False)]}))
    assert "[Music]" not in r.transcript


def test_too_short_transcript_uses_metadata():
    r = fetch_text("v", FakeApi({"v": [FakeTranscript("en", False, "yeah ok")]}))
    assert (r.text_source, r.transcript, r.error) == ("metadata", None, "too_short")


@pytest.mark.parametrize(
    "exc,reason",
    [
        (TranscriptsDisabled("v"), "TranscriptsDisabled"),
        (VideoUnavailable("v"), "VideoUnavailable"),
        (RuntimeError("library changed"), "unexpected:RuntimeError"),
    ],
)
def test_failures_fall_back_with_reason(exc, reason):
    r = fetch_text("v", FakeApi({"v": exc}))
    assert (r.text_source, r.error, r.blocked) == ("metadata", reason, False)


@pytest.mark.parametrize("exc", [RequestBlocked("v"), IpBlocked("v")])
def test_blocking_is_flagged(exc):
    r = fetch_text("v", FakeApi({"v": exc}))
    assert r.blocked and r.error == "blocked"


def test_no_tracks_at_all_falls_back():
    assert fetch_text("v", FakeApi({"v": []})).error == "NoTranscriptFound"


def test_metadata_text():
    assert metadata_text("Title", "Desc") == "Title\n\nDesc"
    assert metadata_text("Title", "") == "Title"


# --- text stage --------------------------------------------------------------------

CH = "UC" + "a" * 22


def seed(conn, n, short=True):
    store.upsert_channel(conn, CH, "A", "python")
    rows = [
        store.VideoRow(f"v{i}", CH, f"t{i}", "d", f"2026-11-1{i % 10}T00:00:00Z", 30, short)
        for i in range(n)
    ]
    store.insert_videos(conn, rows)


def test_text_stage_mixes_transcript_and_metadata(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    seed(conn, 3)
    api = FakeApi({"v0": [FakeTranscript("en", False)], "v1": TranscriptsDisabled("v1")})
    r = text_stage(conn, api=api, interval=0)
    assert (r.processed, r.transcripts, r.metadata) == (3, 1, 2)
    rows = {x["video_id"]: x for x in conn.execute("SELECT * FROM videos")}
    assert rows["v0"]["text_source"] == "transcript" and rows["v0"]["transcript_language"] == "en"
    assert rows["v1"]["transcript_error"] == "TranscriptsDisabled"
    assert all(x["status"] == "text_ready" for x in rows.values())
    # rerun does nothing: every video already has text
    assert text_stage(conn, api=api, interval=0).processed == 0


def test_text_stage_stops_asking_after_repeated_blocks(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    seed(conn, 6)
    api = FakeApi(default=RequestBlocked("x"))
    r = text_stage(conn, api=api, interval=0)
    assert len(api.calls) == MAX_CONSECUTIVE_BLOCKS  # no more requests after that
    assert r.blocked and r.processed == 6 and r.metadata == 6
    assert r.errors == {"blocked": 3, "skipped_ip_blocked": 3}


def test_text_stage_ignores_non_shorts_and_respects_limit(tmp_path):
    conn = store.connect(tmp_path / "t.db")
    seed(conn, 4)
    conn.execute("UPDATE videos SET is_short = 0, status = 'rejected' WHERE video_id = 'v0'")
    conn.commit()
    api = FakeApi(default=[FakeTranscript("en", False)])
    assert text_stage(conn, limit=2, api=api, interval=0).processed == 2
    assert "v0" not in api.calls


def test_clean_caption_text_removes_inline_noise():
    from shorts_digest.transcripts import clean_caption_text

    lines = ["Computers cost an arm and [music] a leg", ">> Cloud engineer.", "[Applause]"]
    assert clean_caption_text(lines) == "Computers cost an arm and a leg Cloud engineer."
