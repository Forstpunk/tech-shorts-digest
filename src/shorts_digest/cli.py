"""Command-line entry point: ``python -m shorts_digest <command>``.

Phase 1: every command loads and validates config, then logs that the stage is not
built yet. Later phases replace the stub bodies with real pipeline calls.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable

from shorts_digest import store
from shorts_digest.config import AppConfig, ConfigError, load_config

log = logging.getLogger("shorts_digest")


def _fetch(cfg: AppConfig, command: str, limit: int | None) -> int:
    """Run the fetch stage inside a recorded run. Imports are local so --help stays fast."""
    from shorts_digest.pipeline import fetch_stage
    from shorts_digest.youtube import QuotaTracker, YouTubeFatalError, build_client

    cfg.require_channels()
    cfg.env.require("YOUTUBE_API_KEY")
    conn = store.connect()
    run_id = store.start_run(conn, command)
    quota = QuotaTracker()
    try:
        result = fetch_stage(cfg, conn, build_client(cfg.env.get("YOUTUBE_API_KEY")), quota, limit)
    except Exception as exc:
        # "Fail loud per run": record the error, then exit non-zero.
        store.finish_run(conn, run_id, "failed", quota_estimate=quota.units, error=str(exc))
        if isinstance(exc, YouTubeFatalError):
            log.error("YouTube API refused the run: %s", exc)
            return 1
        raise
    # A --limit run is "partial": the next run looks back from the last *complete* run.
    status = "success" if result.complete else "partial"
    store.finish_run(
        conn,
        run_id,
        status,
        videos_found=result.videos_found,
        shorts_kept=result.shorts_kept,
        quota_estimate=quota.units,
    )
    log.info(
        "Run %d %s: found=%d kept=%d channels_failed=%d quota=%d units %s",
        run_id,
        status,
        result.videos_found,
        result.shorts_kept,
        result.channels_failed,
        quota.units,
        quota.calls,
    )
    return 0


def _text(limit: int | None) -> int:
    from shorts_digest.pipeline import text_stage

    conn = store.connect()
    r = text_stage(conn, limit)
    log.info(
        "Text stage: processed=%d transcript=%d metadata=%d reasons=%s",
        r.processed,
        r.transcripts,
        r.metadata,
        r.errors,
    )
    return 0


def _extract(cfg: AppConfig, limit: int | None) -> int:
    from shorts_digest.pipeline import extract_stage

    conn = store.connect()
    r = extract_stage(conn, cfg.channels.topic_names, limit)
    log.info(
        "Extract stage: extracted=%d failed=%d usefulness=%s",
        r.processed,
        r.failed,
        dict(sorted(r.by_score.items())),
    )
    return 0


def _not_implemented(stage: str, phase: int) -> int:
    log.info("'%s' is not implemented yet (planned for phase %d).", stage, phase)
    return 0


def cmd_run(cfg: AppConfig, args: argparse.Namespace) -> int:
    # Phases 2-4 exist so far. Phase 5 adds filtering and the digest.
    code = _fetch(cfg, "run", args.limit)
    if code == 0:
        code = _text(args.limit)
    if code == 0:
        code = _extract(cfg, args.limit)
        log.info("Digest stage is not implemented yet (phase 5).")
    return code


def cmd_fetch(cfg: AppConfig, args: argparse.Namespace) -> int:
    return _fetch(cfg, "fetch", args.limit)


def cmd_text(cfg: AppConfig, args: argparse.Namespace) -> int:
    return _text(args.limit)


def cmd_extract(cfg: AppConfig, args: argparse.Namespace) -> int:
    return _extract(cfg, args.limit)


def cmd_digest(cfg: AppConfig, args: argparse.Namespace) -> int:
    return _not_implemented("digest", 5)


def cmd_discover_channels(cfg: AppConfig, args: argparse.Namespace) -> int:
    return _not_implemented("discover-channels", 2)


def _positive_int(value: str) -> int:
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shorts_digest",
        description="Turn new YouTube Shorts from curated channels into a short learning digest.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="full pipeline")
    p.add_argument("--dry-run", action="store_true", help="no email; print plan")
    p.add_argument("--limit", type=_positive_int, help="process at most N videos")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("fetch", help="discovery + classify only")
    p.add_argument("--limit", type=_positive_int, help="process at most N videos")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("text", help="transcript (or title + description) for stored Shorts")
    p.add_argument("--limit", type=_positive_int, help="process at most N videos")
    p.set_defaults(func=cmd_text)

    p = sub.add_parser("extract", help="concept extraction (rule-based) on stored Shorts")
    p.add_argument("--limit", type=_positive_int, help="process at most N videos")
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
