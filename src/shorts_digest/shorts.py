"""Decide whether a video is a YouTube Short.

The API has no "is Short" flag, so the main rule is duration (<= max_short_seconds).
An optional second check asks youtube.com/shorts/<id> directly: Shorts answer 200,
regular videos redirect to /watch. That check is off by default and any failure
counts as "unknown", which falls back to duration alone.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

# ISO 8601 durations as YouTube returns them, e.g. "PT59S", "PT1M5S", "PT1H2M3S", "P1DT2H".
_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_duration(value: str) -> int:
    """Convert an ISO 8601 duration to seconds. Raises ValueError if unparseable."""
    match = _DURATION_RE.match(value or "")
    if not match or value in ("P", "PT"):
        raise ValueError(f"unrecognised duration: {value!r}")
    parts = {k: int(v) if v else 0 for k, v in match.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def is_short_by_duration(duration_seconds: int, max_seconds: int) -> bool:
    # 0 means live streams and upcoming premieres ("P0D"), which are not Shorts.
    return 0 < duration_seconds <= max_seconds


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # returning None stops the redirect
        return None


_opener = urllib.request.build_opener(_NoRedirect)
_last_check = 0.0


def check_shorts_url(video_id: str, min_interval: float = 1.0, timeout: float = 10) -> bool | None:
    """True if /shorts/<id> serves a page, False if it redirects to /watch, None if unknown.

    Rate-limited to one request per ``min_interval`` seconds. This is a plain public
    page request (no login, no cookies), used only as an optional tie-breaker.
    """
    global _last_check
    wait = min_interval - (time.monotonic() - _last_check)
    if wait > 0:
        time.sleep(wait)
    _last_check = time.monotonic()

    req = urllib.request.Request(
        f"https://www.youtube.com/shorts/{video_id}",
        method="HEAD",
        headers={"User-Agent": "shorts-digest/0.1"},
    )
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as err:
        if 300 <= err.code < 400 and "/watch" in (err.headers.get("Location") or ""):
            return False
        log.debug("shorts URL check for %s: HTTP %s -> unknown", video_id, err.code)
        return None
    except (urllib.error.URLError, TimeoutError) as err:
        log.debug("shorts URL check for %s failed (%s) -> unknown", video_id, err)
        return None
