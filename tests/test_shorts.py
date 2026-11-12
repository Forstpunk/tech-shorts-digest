import pytest

from shorts_digest.shorts import is_short_by_duration, parse_duration


@pytest.mark.parametrize(
    "value,seconds",
    [
        ("PT45S", 45),
        ("PT1M", 60),
        ("PT1M5S", 65),
        ("PT3M", 180),
        ("PT1H2M3S", 3723),
        ("P1DT1S", 86401),
        ("P0D", 0),  # live streams / premieres
    ],
)
def test_parse_duration(value, seconds):
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["", "45", "PT", "P", "PTXS", "1M"])
def test_parse_duration_rejects_garbage(value):
    with pytest.raises(ValueError):
        parse_duration(value)


@pytest.mark.parametrize(
    "seconds,expected", [(0, False), (1, True), (180, True), (181, False), (3600, False)]
)
def test_is_short_by_duration(seconds, expected):
    assert is_short_by_duration(seconds, 180) is expected
