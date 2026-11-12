"""Load and validate configuration from config/*.yaml and .env.

Design notes:
- Every model uses ``extra="forbid"`` so a typo in YAML (e.g. ``digest_szie``) is an
  error instead of being silently ignored.
- Secrets are NOT required at load time. Each pipeline stage asks for the keys it needs
  via ``Env.require(...)``. That way ``--help``, ``--dry-run`` and the tests work
  without a ``.env`` file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"


class ConfigError(Exception):
    """Raised for any broken or missing configuration. The message is meant for humans."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- channels.yaml -----------------------------------------------------------------


class Channel(_Strict):
    # YouTube channel IDs are "UC" + 22 characters. Checking the shape catches
    # copy-paste mistakes like pasting a handle (@name) or a full URL.
    id: str = Field(pattern=r"^UC[A-Za-z0-9_-]{22}$")
    name: str


class ChannelsConfig(_Strict):
    topics: dict[str, list[Channel]]

    @property
    def topic_names(self) -> list[str]:
        return list(self.topics)

    def all_channels(self) -> list[tuple[str, Channel]]:
        """Flat list of (topic, channel) pairs."""
        return [(topic, ch) for topic, chans in self.topics.items() for ch in chans]

    @model_validator(mode="after")
    def _no_duplicate_ids(self) -> ChannelsConfig:
        seen: set[str] = set()
        for _, ch in self.all_channels():
            if ch.id in seen:
                raise ValueError(f"channel {ch.id} ({ch.name}) is listed more than once")
            seen.add(ch.id)
        return self


# --- settings.yaml -----------------------------------------------------------------


class EmailConfig(_Strict):
    enabled: bool = False


class QuotaConfig(_Strict):
    # Our own ceiling, below Google's 10,000/day, so a bug can't burn the whole quota.
    daily_limit: int = Field(default=6000, gt=0, le=10000)
    # Units kept back for the curated fetch; discovery only spends what is left.
    curated_reserve: int = Field(default=500, ge=0)

    @model_validator(mode="after")
    def _reserve_fits(self) -> QuotaConfig:
        if self.curated_reserve > self.daily_limit:
            raise ValueError("curated_reserve cannot be larger than daily_limit")
        return self


class DiscoveryConfig(_Strict):
    """Capped daily keyword search for small, rising creators (CLAUDE.md section 6a)."""

    enabled: bool = False
    # search.list costs 100 units each; CLAUDE.md caps discovery at 15 a day.
    max_searches_per_day: int = Field(default=15, ge=0, le=15)
    lookback_days: int = Field(default=7, gt=0, le=30)
    relevance_language: str = "en"
    safe_search: Literal["none", "moderate", "strict"] = "moderate"
    min_subscribers: int = Field(default=1000, ge=0)
    max_subscribers: int = Field(default=100_000, gt=0)
    min_uploads: int = Field(default=5, ge=0)
    min_views: int = Field(default=5000, ge=0)
    min_views_per_subscriber: float = Field(default=2.0, ge=0)
    max_candidates_per_day: int = Field(default=20, gt=0)
    channel_stats_cache_days: int = Field(default=7, gt=0)
    keywords: list[str] = []

    @model_validator(mode="after")
    def _sane(self) -> DiscoveryConfig:
        if self.min_subscribers > self.max_subscribers:
            raise ValueError("min_subscribers cannot be larger than max_subscribers")
        if self.enabled and not self.keywords:
            raise ValueError("discovery is enabled but has no keywords")
        if len(set(k.lower() for k in self.keywords)) != len(self.keywords):
            raise ValueError("discovery keywords contain a duplicate")
        return self


class Settings(_Strict):
    lookback_days: int = Field(gt=0)
    max_short_seconds: int = Field(gt=0)
    verify_shorts_url: bool = False
    min_usefulness: int = Field(ge=1, le=5)
    digest_size: int = Field(gt=0)
    max_per_topic: int = Field(gt=0)
    quota: QuotaConfig = QuotaConfig()
    discovery: DiscoveryConfig = DiscoveryConfig()
    email: EmailConfig = EmailConfig()


# --- .env --------------------------------------------------------------------------

ENV_KEYS = (
    "YOUTUBE_API_KEY",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_APP_PASSWORD",
    "DIGEST_TO",
)


class Env:
    """Read-only view of the environment variables this project uses."""

    def __init__(self, values: dict[str, str]):
        self._values = values

    def get(self, key: str) -> str | None:
        return self._values.get(key) or None

    def require(self, *keys: str) -> None:
        """Fail fast if any of ``keys`` is missing. Never prints the values."""
        missing = [k for k in keys if not self.get(k)]
        if missing:
            raise ConfigError(
                f"Missing environment variable(s): {', '.join(missing)}. "
                "Copy .env.example to .env and fill them in "
                "(or set them as GitHub repository secrets)."
            )

    def __repr__(self) -> str:  # avoid leaking secrets if someone logs this object
        present = [k for k in ENV_KEYS if self.get(k)]
        return f"Env(set={present})"


# --- loading -----------------------------------------------------------------------


class AppConfig(BaseModel):
    settings: Settings
    channels: ChannelsConfig
    env: Env

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def require_channels(self) -> None:
        if not self.channels.all_channels():
            raise ConfigError(
                "config/channels.yaml has no channels. Add at least one real channel ID "
                "under a topic, then run again."
            )


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a YAML mapping at the top level")
    return data


def _validate(model: type[BaseModel], data: dict, filename: str) -> BaseModel:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        # Turn pydantic's error list into one readable line per problem.
        lines = [
            f"  - {'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
            for err in exc.errors()
        ]
        raise ConfigError(f"{filename} is invalid:\n" + "\n".join(lines)) from exc


def load_config(config_dir: Path = DEFAULT_CONFIG_DIR, env_file: Path | None = None) -> AppConfig:
    """Load settings.yaml, channels.yaml and .env. Raises ConfigError on any problem."""
    settings = _validate(Settings, _read_yaml(config_dir / "settings.yaml"), "settings.yaml")
    channels = _validate(ChannelsConfig, _read_yaml(config_dir / "channels.yaml"), "channels.yaml")

    # override=False: real environment variables (e.g. GitHub secrets) win over .env.
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    env = Env({k: os.environ.get(k, "") for k in ENV_KEYS})

    return AppConfig(settings=settings, channels=channels, env=env)
