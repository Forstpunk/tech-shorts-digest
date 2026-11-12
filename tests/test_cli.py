"""CLI wiring. Uses the committed config/ (channels.yaml ships empty)."""

import pytest

from shorts_digest.cli import main


@pytest.fixture(autouse=True)
def no_real_env(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)


def test_help_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "fetch" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [["extract"], ["digest", "--days", "7"], ["discover-channels", "data engineering"]],
)
def test_stub_commands_exit_zero(argv):
    assert main(argv) == 0


def test_fetch_stops_when_channels_empty(caplog):
    assert main(["fetch"]) == 2
    assert "no channels" in caplog.text
