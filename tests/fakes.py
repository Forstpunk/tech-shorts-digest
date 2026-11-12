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
    def __init__(self, fn, kwargs):
        self._fn, self._kwargs = fn, kwargs

    def execute(self, num_retries: int = 0):
        return self._fn(**self._kwargs)


class _Resource:
    def __init__(self, fn, log, name):
        self._fn, self._log, self._name = fn, log, name

    def list(self, **kwargs):
        self._log.append((self._name, kwargs))
        return _Request(self._fn, kwargs)


class FakeYouTube:
    """
    channels:  {channel_id: uploads_playlist_id}
    playlists: {playlist_id: [ {videoId, videoPublishedAt}, ... ] newest first} or an Exception
    videos:    {video_id: {channelId, title, duration, publishedAt}}
    """

    def __init__(self, channels=None, playlists=None, videos=None, page_size=50):
        self.channels_data = channels or {}
        self.playlists_data = playlists or {}
        self.videos_data = videos or {}
        self.page_size = page_size
        self.calls: list[tuple[str, dict]] = []

    def channels(self):
        def fn(id, **_):
            items = [
                {
                    "id": cid,
                    "contentDetails": {"relatedPlaylists": {"uploads": self.channels_data[cid]}},
                }
                for cid in id.split(",")
                if cid in self.channels_data
            ]
            return {"items": items}

        return _Resource(fn, self.calls, "channels.list")

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

        return _Resource(fn, self.calls, "playlistItems.list")

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
                            },
                            "contentDetails": {"duration": v["duration"]},
                        }
                    )
            return {"items": items}

        return _Resource(fn, self.calls, "videos.list")

    def count(self, method: str) -> int:
        return sum(1 for name, _ in self.calls if name == method)
