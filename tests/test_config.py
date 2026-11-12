"""Config loading: good config loads, broken config fails with a readable message."""

from pathlib import Path

import pytest

from shorts_digest.config import ConfigError, Env, load_config

# Obviously fake channel ID: right shape, not a real channel.
FAKE_ID = "UC" + "x" * 22

SETTINGS = """\
lookback_days: 3
max_short_seconds: 180
verify_shorts_url: false
min_usefulness: 3
digest_size: 8
max_per_topic: 3
llm:
  primary: {provider: gemini, model: test-model}
  fallback: {provider: groq, model: test-model}
  requests_per_minute: 10
email:
  enabled: false
"""

CHANNELS = f"""\
topics:
  python:
    - id: {FAKE_ID}
      name: Fake Channel
  data_engineering: []
"""


def write_config(tmp_path: Path, settings: str = SETTINGS, channels: str = CHANNELS) -> Path:
    (tmp_path / "settings.yaml").write_text(settings)
    (tmp_path / "channels.yaml").write_text(channels)
    return tmp_path


@pytest.fixture(autouse=True)
def no_real_env(monkeypatch, tmp_path):
    """Keep the developer's real .env / shell secrets out of tests."""
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)


def load(tmp_path: Path):
    return load_config(tmp_path, env_file=tmp_path / "missing.env")


def test_valid_config_loads(tmp_path):
    cfg = load(write_config(tmp_path))
    assert cfg.settings.digest_size == 8
    assert cfg.channels.topic_names == ["python", "data_engineering"]
    assert cfg.channels.all_channels()[0][1].id == FAKE_ID


def test_repo_config_files_are_valid():
    """The committed config/ files must always pass validation."""
    load_config(env_file=Path("does-not-exist.env"))


def test_unknown_key_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="digest_szie"):
        load(write_config(tmp_path, settings=SETTINGS + "digest_szie: 5\n"))


def test_missing_key_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="lookback_days"):
        load(write_config(tmp_path, settings=SETTINGS.replace("lookback_days: 3\n", "")))


def test_bad_yaml_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="not valid YAML"):
        load(write_config(tmp_path, channels="topics: [unclosed"))


def test_bad_channel_id_is_rejected(tmp_path):
    bad = CHANNELS.replace(FAKE_ID, "somehandle")
    with pytest.raises(ConfigError, match="channels.yaml is invalid"):
        load(write_config(tmp_path, channels=bad))


def test_duplicate_channel_is_rejected(tmp_path):
    dup = CHANNELS.replace(
        "data_engineering: []", f"data_engineering:\n    - id: {FAKE_ID}\n      name: Again"
    )
    with pytest.raises(ConfigError, match="more than once"):
        load(write_config(tmp_path, channels=dup))


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path)


def test_env_require_names_missing_keys_without_values():
    env = Env({"YOUTUBE_API_KEY": "secret-value"})
    env.require("YOUTUBE_API_KEY")
    with pytest.raises(ConfigError, match="GEMINI_API_KEY") as exc:
        env.require("GEMINI_API_KEY")
    assert "secret-value" not in str(exc.value)
    assert "secret-value" not in repr(env)
