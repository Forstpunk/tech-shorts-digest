"""One quota guard for every YouTube API call.

The API gives 10,000 units a day. We set our own lower ceiling (``quota.daily_limit``,
default 6,000) so a bug can never burn the whole day's quota. Every call goes through
``QuotaGuard.spend()``, which:

1. knows what the method costs (``COSTS``),
2. refuses the call *before* it is made if it would push today's total over the limit,
3. records it in the ``quota_usage`` table, so separate runs on the same day add up.

"Today" follows Pacific time, because that is when Google resets the quota.

Sources share the same daily pot, but curated comes first: ``curated_reserve`` units are
kept back for it, so discovery (and the resolve helper) can only spend what is left.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from shorts_digest import store

PACIFIC = ZoneInfo("America/Los_Angeles")

# Units per call (YouTube Data API v3 quota table).
COSTS: dict[str, int] = {
    "channels.list": 1,
    "playlistItems.list": 1,
    "videos.list": 1,
    "search.list": 100,
}

SOURCES = ("curated", "discovery", "resolve")


class QuotaLimitError(RuntimeError):
    """A call was refused because it would exceed our own daily limit. Nothing was spent."""


def quota_day(now: datetime | None = None) -> str:
    """The quota day (YYYY-MM-DD in Pacific time) that ``now`` falls in."""
    return (now or datetime.now(PACIFIC)).astimezone(PACIFIC).date().isoformat()


class QuotaGuard:
    def __init__(
        self,
        conn: sqlite3.Connection,
        source: str,
        daily_limit: int,
        curated_reserve: int = 0,
        clock: Callable[[], datetime] | None = None,
    ):
        if source not in SOURCES:
            raise ValueError(f"unknown quota source {source!r}")
        self.conn = conn
        self.source = source
        self.daily_limit = daily_limit
        self.curated_reserve = curated_reserve
        self._clock = clock or (lambda: datetime.now(PACIFIC))
        # What *this* run spent, for the summary line.
        self.units = 0
        self.calls: dict[str, int] = {}

    @property
    def day(self) -> str:
        return quota_day(self._clock())

    def used_today(self, source: str | None = None) -> int:
        return store.quota_used(self.conn, self.day, source)

    def allowance(self) -> int:
        """Most units today's total may reach for this source."""
        if self.source == "curated":
            return self.daily_limit
        # Keep back whatever part of the curated reserve curated hasn't used yet today.
        unused_reserve = max(self.curated_reserve - self.used_today("curated"), 0)
        return self.daily_limit - unused_reserve

    def remaining(self) -> int:
        return max(self.allowance() - self.used_today(), 0)

    def spend(self, method: str) -> None:
        """Record one call of ``method``, or raise QuotaLimitError without recording it."""
        cost = COSTS.get(method)
        if cost is None:
            raise ValueError(f"no known quota cost for {method!r}; add it to COSTS")
        if self.used_today() + cost > self.allowance():
            raise QuotaLimitError(
                f"{method} ({cost} units) refused: {self.source} may use up to "
                f"{self.allowance()} units today and {self.used_today()} are used "
                f"(daily_limit {self.daily_limit} in settings.yaml)"
            )
        store.add_quota_usage(self.conn, self.day, self.source, method, cost)
        self.units += cost
        self.calls[method] = self.calls.get(method, 0) + 1
