"""Command-line entry point: ``python -m shorts_digest <command>``.

Phase 1: every command loads and validates config, then logs that the stage is not
built yet. Later phases replace the stub bodies with real pipeline calls.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable

from shorts_digest.config import AppConfig, ConfigError, load_config

log = logging.getLogger("shorts_digest")


def _not_implemented(stage: str, phase: int) -> int:
    log.info("'%s' is not implemented yet (planned for phase %d).", stage, phase)
    return 0


def cmd_run(cfg: AppConfig, args: argparse.Namespace) -> int:
    cfg.require_channels()
    if not args.dry_run:
        cfg.env.require("YOUTUBE_API_KEY")
    return _not_implemented("run", 2)


def cmd_fetch(cfg: AppConfig, args: argparse.Namespace) -> int:
    cfg.require_channels()
    cfg.env.require("YOUTUBE_API_KEY")
    return _not_implemented("fetch", 2)


def cmd_extract(cfg: AppConfig, args: argparse.Namespace) -> int:
    return _not_implemented("extract", 4)


def cmd_digest(cfg: AppConfig, args: argparse.Namespace) -> int:
    return _not_implemented("digest", 5)


def cmd_discover_channels(cfg: AppConfig, args: argparse.Namespace) -> int:
    return _not_implemented("discover-channels", 2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shorts_digest",
        description="Turn new YouTube Shorts from curated channels into a short learning digest.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="full pipeline")
    p.add_argument("--dry-run", action="store_true", help="no LLM calls, no email; print plan")
    p.add_argument("--limit", type=int, help="process at most N videos")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("fetch", help="discovery + classify only")
    p.add_argument("--limit", type=int, help="process at most N videos")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("extract", help="LLM stage on stored, unprocessed videos")
    p.add_argument("--limit", type=int, help="process at most N videos")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("digest", help="rebuild a digest from stored data")
    p.add_argument("--days", type=int, default=1, help="look back N days (default 1)")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("discover-channels", help="one-off helper to find channel IDs")
    p.add_argument("query", help='e.g. "data engineering"')
    p.set_defaults(func=cmd_discover_channels)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    handler: Callable[[AppConfig, argparse.Namespace], int] = args.func
    try:
        cfg = load_config()
        return handler(cfg, args)
    except ConfigError as exc:
        # "Fail loud per run": a broken config stops everything with a clear message.
        log.error("%s", exc)
        return 2
