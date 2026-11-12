"""CLI wiring. Uses the committed config/ (channels.yaml ships empty)."""

import pytest

from shorts_digest.cli import main


@pytest.fixture(autouse=True)
def no_real_env(monkeypatch, tmp_path):
    """Never read the developer's real .env in tests."""
    import shorts_digest.cli as cli
    from shorts_digest.config import load_config

    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr(cli, "load_config", lambda: load_config(env_file=tmp_path / "none.env"))


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


def test_fetch_stops_when_channels_empty(caplog, monkeypatch, tmp_path):
    from pathlib import Path

    import shorts_digest.cli as cli
    from shorts_digest.config import load_config

    real = Path(__file__).resolve().parents[1] / "config"
    (tmp_path / "settings.yaml").write_text((real / "settings.yaml").read_text())
    (tmp_path / "channels.yaml").write_text("topics:\n  python: []\n")
    monkeypatch.setattr(cli, "load_config", lambda: load_config(tmp_path, tmp_path / "none.env"))
    assert main(["fetch"]) == 2
    assert "no channels" in caplog.text


def test_fetch_stops_without_api_key(caplog, monkeypatch, tmp_path):
    import shorts_digest.cli as cli
    from shorts_digest.config import load_config

    # Shipped config has channels; point .env at a missing file so no key is found.
    monkeypatch.setattr(cli, "load_config", lambda: load_config(env_file=tmp_path / "none.env"))
    assert main(["fetch"]) == 2
    assert "YOUTUBE_API_KEY" in caplog.text
