"""YouTube Data API v3 calls: channel -> uploads playlist -> recent videos -> durations.

Quota (default 10,000 units/day): every call used here costs 1 unit. ``search.list``
(100 units) is deliberately never used in the daily pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

MAX_IDS_PER_CALL = 50  # API limit for channels.list / videos.list id= and maxResults
NUM_RETRIES = 2  # client retries 5xx / network errors with backoff

# Errors that mean the whole run can't work (bad key, quota gone): fail loud.
_FATAL_REASONS = {
    "quotaExceeded",
    "dailyLimitExceeded",
    "keyInvalid",
    "keyExpired",
    "accessNotConfigured",
    "forbidden",
    "ipRefererBlocked",
}


class YouTubeFatalError(RuntimeError):
    """The API refused the run itself (quota, key, permissions). Stop everything."""


@dataclass
class QuotaTracker:
    units: int = 0
    calls: dict[str, int] = field(default_factory=dict)

    def add(self, method: str, units: int = 1) -> None:
        self.units += units
        self.calls[method] = self.calls.get(method, 0) + 1


@dataclass
class PlaylistItem:
    video_id: str
    published_at: datetime


@dataclass
class VideoDetails:
    video_id: str
    channel_id: str
    title: str
    description: str
    published_at: str
    duration: str  # raw ISO 8601, e.g. "PT45S"


def build_client(api_key: str) -> Any:
    # cache_discovery=False avoids a noisy warning about the file cache.
    return build("youtube", "v3", developerKey=api_key, cache_discovery=False)


def _reason(err: HttpError) -> str:
    try:
        return err.error_details[0].get("reason", "")  # type: ignore[index]
    except (IndexError, TypeError, AttributeError):
        return ""


def _execute(request: Any, quota: QuotaTracker, method: str) -> dict:
    """Run one API request, count its quota, and turn run-level errors into YouTubeFatalError."""
    quota.add(method)
    try:
        return request.execute(num_retries=NUM_RETRIES)
    except HttpError as err:
        reason = _reason(err)
        if reason in _FATAL_REASONS or err.status_code in (400, 401):
            # Message from Google is safe to log; it never contains the key.
            raise YouTubeFatalError(f"{method}: HTTP {err.status_code} {reason}") from err
        raise


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def resolve_uploads_playlists(
    client: Any, channel_ids: list[str], quota: QuotaTracker
) -> dict[str, str]:
    """channel_id -> uploads playlist ID, 50 channels per call. Missing channels are omitted."""
    result: dict[str, str] = {}
    for i in range(0, len(channel_ids), MAX_IDS_PER_CALL):
        batch = channel_ids[i : i + MAX_IDS_PER_CALL]
        resp = _execute(
            client.channels().list(
                part="contentDetails", id=",".join(batch), maxResults=MAX_IDS_PER_CALL
            ),
            quota,
            "channels.list",
        )
        for item in resp.get("items", []):
            result[item["id"]] = item["contentDetails"]["relatedPlaylists"]["uploads"]
    return result


def list_recent_uploads(
    client: Any, playlist_id: str, since: datetime, quota: QuotaTracker, max_pages: int = 3
) -> list[PlaylistItem]:
    """Videos in an uploads playlist published at or after ``since``.

    Uploads playlists are newest-first, so we stop paging at the first older video.
    ``max_pages`` (50 items each) caps the cost if a channel has a huge burst.
    """
    items: list[PlaylistItem] = []
    page_token: str | None = None
    for _ in range(max_pages):
        resp = _execute(
            client.playlistItems().list(
                part="contentDetails",
                playlistId=playlist_id,
                maxResults=MAX_IDS_PER_CALL,
                pageToken=page_token,
            ),
            quota,
            "playlistItems.list",
        )
        reached_older = False
        for it in resp.get("items", []):
            details = it["contentDetails"]
            # videoPublishedAt is missing for private/deleted videos; skip those.
            if "videoPublishedAt" not in details:
                continue
            published = _parse_ts(details["videoPublishedAt"])
            if published < since:
                reached_older = True
                continue
            items.append(PlaylistItem(details["videoId"], published))
        page_token = resp.get("nextPageToken")
        if reached_older or not page_token:
            break
    return items


def fetch_video_details(
    client: Any, video_ids: list[str], quota: QuotaTracker
) -> dict[str, VideoDetails]:
    """Title, description and duration for up to 50 videos per call."""
    result: dict[str, VideoDetails] = {}
    for i in range(0, len(video_ids), MAX_IDS_PER_CALL):
        batch = video_ids[i : i + MAX_IDS_PER_CALL]
        resp = _execute(
            client.videos().list(
                part="snippet,contentDetails", id=",".join(batch), maxResults=MAX_IDS_PER_CALL
            ),
            quota,
            "videos.list",
        )
        for item in resp.get("items", []):
            snip = item["snippet"]
            result[item["id"]] = VideoDetails(
                video_id=item["id"],
                channel_id=snip["channelId"],
                title=snip.get("title", ""),
                description=snip.get("description", ""),
                published_at=snip.get("publishedAt", ""),
                duration=item["contentDetails"].get("duration", ""),
            )
    return result
