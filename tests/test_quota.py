"""Quota guard: costs, the daily limit, the curated reserve, the Pacific-time day."""

from datetime import UTC, datetime

import pytest

from shorts_digest import store
from shorts_digest.quota import QuotaGuard, QuotaLimitError, quota_day


@pytest.fixture
def conn(tmp_path):
    return store.connect(tmp_path / "q.db")


def test_spend_records_units_per_method(conn):
    g = QuotaGuard(conn, "curated", daily_limit=100)
    g.spend("videos.list")
    g.spend("videos.list")
    g.spend("channels.list")
    assert (g.units, g.calls) == (3, {"videos.list": 2, "channels.list": 1})
    rows = {r["method"]: (r["calls"], r["units"]) for r in store.quota_breakdown(conn, g.day)}
    assert rows == {"videos.list": (2, 2), "channels.list": (1, 1)}


def test_usage_adds_up_across_runs_on_the_same_day(conn):
    QuotaGuard(conn, "curated", 100).spend("videos.list")
    second = QuotaGuard(conn, "curated", 100)
    second.spend("videos.list")
    assert second.units == 1  # this run
    assert second.used_today() == 2  # the whole day


def test_call_over_the_limit_is_refused_before_it_is_recorded(conn):
    g = QuotaGuard(conn, "curated", daily_limit=2)
    g.spend("videos.list")
    g.spend("videos.list")
    with pytest.raises(QuotaLimitError, match="daily_limit 2"):
        g.spend("videos.list")
    assert g.used_today() == 2


def test_search_costs_100(conn):
    g = QuotaGuard(conn, "discovery", daily_limit=1000)
    g.spend("search.list")
    assert g.used_today() == 100


def test_unknown_method_is_an_error(conn):
    with pytest.raises(ValueError, match="COSTS"):
        QuotaGuard(conn, "curated", 100).spend("comments.list")


def test_discovery_cannot_eat_the_curated_reserve(conn):
    disc = QuotaGuard(conn, "discovery", daily_limit=250, curated_reserve=100)
    assert disc.allowance() == 150
    disc.spend("search.list")  # 100
    with pytest.raises(QuotaLimitError):
        disc.spend("search.list")  # would reach 200 > 150
    # Curated can still use the full limit.
    cur = QuotaGuard(conn, "curated", daily_limit=250, curated_reserve=100)
    assert cur.remaining() == 150


def test_reserve_is_released_as_curated_uses_it(conn):
    cur = QuotaGuard(conn, "curated", daily_limit=250, curated_reserve=100)
    for _ in range(100):
        cur.spend("videos.list")
    disc = QuotaGuard(conn, "discovery", daily_limit=250, curated_reserve=100)
    assert disc.allowance() == 250  # curated already took its share
    disc.spend("search.list")
    assert disc.remaining() == 50


def test_new_pacific_day_starts_from_zero(conn):
    t = {"now": datetime(2026, 11, 12, 7, 59, tzinfo=UTC)}  # 23:59 Pacific on the 11th
    g = QuotaGuard(conn, "curated", daily_limit=1, clock=lambda: t["now"])
    g.spend("videos.list")
    with pytest.raises(QuotaLimitError):
        g.spend("videos.list")
    t["now"] = datetime(2026, 11, 12, 8, 1, tzinfo=UTC)  # 00:01 Pacific on the 12th
    g.spend("videos.list")
    assert g.day == "2026-11-12"


def test_quota_day_uses_pacific_time():
    assert quota_day(datetime(2026, 11, 12, 3, 0, tzinfo=UTC)) == "2026-11-11"


def test_unknown_source_rejected(conn):
    with pytest.raises(ValueError):
        QuotaGuard(conn, "other", 100)
