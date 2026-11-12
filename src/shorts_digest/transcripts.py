"""Fetch a Short's transcript, falling back to its title + description.

``youtube-transcript-api`` is unofficial: it reads the captions YouTube's own player uses.
It can break, and YouTube often blocks it from cloud IPs (e.g. GitHub Actions). So every
call is wrapped, a failure never stops the run, and the reason is recorded.

Language preference (CLAUDE.md section 6): English, then Hindi, then any auto-generated track.
Within a language, human-made captions win over auto-generated ones.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from youtube_transcript_api import (
    CouldNotRetrieveTranscript,
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    YouTubeTranscriptApi,
)

log = logging.getLogger(__name__)

# YouTube uses regional codes too, so list the common English variants explicitly.
PREFERRED_LANGUAGES = ["en", "en-US", "en-GB", "en-IN", "hi"]
MAX_TRANSCRIPT_CHARS = 4000  # Shorts are short; this also keeps the DB small
MIN_TRANSCRIPT_WORDS = 8  # fewer words than this is usually music lyrics or "[Music]"
# Caption noise: "[Music]" / "[Applause]" tags and ">>" speaker-change markers.
_NOISE_RE = re.compile(r"\[[^\]]*\]|>>")


@dataclass
class TextResult:
    text_source: str  # "transcript" or "metadata"
    transcript: str | None
    language: str | None = None
    error: str | None = None  # why the transcript wasn't used (stored for debugging)
    blocked: bool = False  # YouTube refused the request (IP block), not a per-video issue


def _pick_transcript(transcripts: Any) -> Any:
    """English/Hindi (manual before generated), else the first auto-generated track."""
    try:
        return transcripts.find_transcript(PREFERRED_LANGUAGES)
    except NoTranscriptFound:
        pass
    for t in transcripts:
        if t.is_generated:
            return t
    raise NoTranscriptFound("", PREFERRED_LANGUAGES, transcripts)


def fetch_text(video_id: str, api: Any | None = None) -> TextResult:
    """Try the transcript; on any failure return a metadata result with the reason."""
    api = api or YouTubeTranscriptApi()
    try:
        transcript = _pick_transcript(api.list(video_id))
        fetched = transcript.fetch()
    except (RequestBlocked, IpBlocked):
        return TextResult("metadata", None, error="blocked", blocked=True)
    except CouldNotRetrieveTranscript as exc:
        # e.g. TranscriptsDisabled, NoTranscriptFound, VideoUnavailable, AgeRestricted
        return TextResult("metadata", None, error=type(exc).__name__)
    except Exception as exc:  # the library is unofficial; never let it crash the run
        log.warning("Transcript for %s failed unexpectedly: %r", video_id, exc)
        return TextResult("metadata", None, error=f"unexpected:{type(exc).__name__}")

    text = clean_caption_text(s.text for s in fetched.snippets)
    if len(text.split()) < MIN_TRANSCRIPT_WORDS:
        return TextResult("metadata", None, language=transcript.language_code, error="too_short")
    return TextResult("transcript", text, language=transcript.language_code)


def clean_caption_text(lines: Any) -> str:
    """Join timed caption lines into one string without [Music] tags or >> markers."""
    text = _NOISE_RE.sub(" ", " ".join(lines))
    return " ".join(text.split())[:MAX_TRANSCRIPT_CHARS]


def metadata_text(title: str, description: str) -> str:
    """The fallback text the LLM sees when there is no usable transcript."""
    return f"{title}\n\n{description}".strip()


class Throttle:
    """Wait so that calls are at least ``interval`` seconds apart (be polite, avoid blocks)."""

    def __init__(self, interval: float):
        self.interval = interval
        self._last = 0.0

    def wait(self) -> None:
        delay = self.interval - (time.monotonic() - self._last)
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()
