"""A tiny stand-in for the googleapiclient YouTube client. No network, no quota.

Mimics the call chain the code uses: client.channels().list(**kw).execute(num_retries=...)
"""

from __future__ import annotations

import json

import httplib2
from googleapiclient.errors import HttpError

# Obviously fake IDs: right shape, not real channels/videos.
CH_A = "UC" + "a" * 22
CH_B = "UC" + "b" * 22


def http_error(status: int, reason: str = "") -> HttpError:
    body = {"error": {"code": status, "message": "fake", "errors": [{"reason": reason}]}}
    return HttpError(httplib2.Response({"status": status}), json.dumps(body).encode())


class _Request:
    def __init__(self, fn, kwargs, pending_failures, log, name):
        self._fn, self._kwargs, self._failures = fn, kwargs, pending_failures
        self._log, self._name = log, name

    def execute(self, num_retries: int = 0):
        self._log.append((self._name, self._kwargs))  # one entry per attempt
        if self._failures:  # injected errors are raised first, one per attempt
            raise self._failures.pop(0)
        return self._fn(**self._kwargs)


class _Resource:
    def __init__(self, fn, log, name, failures):
        self._fn, self._log, self._name, self._failures = fn, log, name, failures

    def list(self, **kwargs):
        failures = self._failures.setdefault(self._name, [])
        return _Request(self._fn, kwargs, failures, self._log, self._name)


class FakeYouTube:
    """
    channels:  {channel_id: uploads_playlist_id}
    playlists: {playlist_id: [ {videoId, videoPublishedAt}, ... ] newest first} or an Exception
    videos:    {video_id: {channelId, title, duration, publishedAt}}
    info:      {channel_id: {title, handle, subs, videos, hidden}} - snippet/statistics
    search:    {query: [channel_id, ...]} - search.list type=channel results
    video_search: {query: [video_id, ...]} - search.list type=video results (from videos)
    failures:  {"videos.list": [exc, exc]} - raised on the next attempts of that method
    """

    def __init__(
        self,
        channels=None,
        playlists=None,
        videos=None,
        page_size=50,
        info=None,
        search=None,
        video_search=None,
    ):
        self.channels_data = channels or {}
        self.playlists_data = playlists or {}
        self.videos_data = videos or {}
        self.page_size = page_size
        self.channel_info = info or {}  # {cid: {title, handle, subs, videos, hidden}}
        self.search_results = search or {}  # {query: [cid, ...]}
        self.video_search = video_search or {}  # {query: [video_id, ...]}
        self.calls: list[tuple[str, dict]] = []
        self.failures: dict[str, list[Exception]] = {}

    def channels(self):
        def fn(id=None, forHandle=None, **_):
            if forHandle is not None:  # handles are case-insensitive on YouTube
                ids = [
                    cid
                    for cid, info in self.channel_info.items()
                    if info.get("handle", "").lower() == forHandle.lower()
                ]
            else:
                ids = id.split(",")
            items = []
            for cid in ids:
                if cid not in self.channels_data and cid not in self.channel_info:
                    continue
                item = {"id": cid}
                if cid in self.channels_data:
                    uploads = self.channels_data[cid]
                    item["contentDetails"] = {"relatedPlaylists": {"uploads": uploads}}
                if cid in self.channel_info:
                    info = self.channel_info[cid]
                    item["snippet"] = {"title": info["title"], "customUrl": info.get("handle")}
                    item["statistics"] = {
                        "subscriberCount": str(info.get("subs", 5000)),
                        "videoCount": str(info.get("videos", 100)),
                        "hiddenSubscriberCount": info.get("hidden", False),
                    }
                items.append(item)
            return {"items": items}

        return _Resource(fn, self.calls, "channels.list", self.failures)

    def search(self):
        def fn(q, maxResults=5, type="channel", **_):
            if type == "video":
                items = []
                for vid in self.video_search.get(q, [])[:maxResults]:
                    v = self.videos_data.get(vid, {})
                    snippet = {
                        "channelId": v.get("channelId", ""),
                        "title": v.get("title", vid),
                        "publishedAt": v.get("publishedAt", ""),
                    }
                    items.append(
                        {"id": {"kind": "youtube#video", "videoId": vid}, "snippet": snippet}
                    )
                return {"items": items}
            ids = self.search_results.get(q, [])[:maxResults]
            return {"items": [{"id": {"kind": "youtube#channel", "channelId": c}} for c in ids]}

        return _Resource(fn, self.calls, "search.list", self.failures)

    def playlistItems(self):
        def fn(playlistId, pageToken=None, **_):
            data = self.playlists_data.get(playlistId, [])
            if isinstance(data, Exception):
                raise data
            start = int(pageToken or 0)
            page = data[start : start + self.page_size]
            resp = {"items": [{"contentDetails": d} for d in page]}
            if start + self.page_size < len(data):
                resp["nextPageToken"] = str(start + self.page_size)
            return resp

        return _Resource(fn, self.calls, "playlistItems.list", self.failures)

    def videos(self):
        def fn(id, **_):
            items = []
            for vid in id.split(","):
                v = self.videos_data.get(vid)
                if v:
                    items.append(
                        {
                            "id": vid,
                            "snippet": {
                                "channelId": v["channelId"],
                                "title": v.get("title", vid),
                                "description": v.get("description", ""),
                                "publishedAt": v["publishedAt"],
                                "defaultAudioLanguage": v.get("defaultAudioLanguage"),
                            },
                            "contentDetails": {"duration": v["duration"]},
                            "statistics": {"viewCount": str(v["views"])} if "views" in v else {},
                        }
                    )
            return {"items": items}

        return _Resource(fn, self.calls, "videos.list", self.failures)

    def count(self, method: str) -> int:  # attempts, including retried ones
        return sum(1 for name, _ in self.calls if name == method)


def make_guard(conn, daily_limit: int = 6000, source: str = "curated", reserve: int = 500):
    from shorts_digest.quota import QuotaGuard

    return QuotaGuard(conn, source, daily_limit, reserve)
