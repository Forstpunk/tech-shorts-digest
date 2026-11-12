"""Command-line entry point: ``python -m shorts_digest <command>``.

``run`` and ``fetch`` are *recorded* runs: they get a row in the ``runs`` table, every
stage runs inside one try/except, and a crash in any stage is written to that row
(status, stage, error) before the command exits non-zero. Stages save each video as
they go, so the next run simply carries on from where a failed one stopped.
"""

from __future__ import annotations

import argparse
import logging
import math
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from shorts_digest import store
from shorts_digest.config import AppConfig, ConfigError, load_config

log = logging.getLogger("shorts_digest")


# --- stage helpers (each logs its own detail line) ---------------------------------


def _new_guard(cfg: AppConfig, conn: sqlite3.Connection, source: str = "curated"):
    from shorts_digest.quota import QuotaGuard

    q = cfg.settings.quota
    return QuotaGuard(conn, source, q.daily_limit, q.curated_reserve)


def _fetch(cfg: AppConfig, conn: sqlite3.Connection, guard, limit: int | None):
    from shorts_digest import youtube
    from shorts_digest.pipeline import fetch_stage

    client = youtube.build_client(cfg.env.get("YOUTUBE_API_KEY"))
    r = fetch_stage(cfg, conn, client, guard, limit)
    log.info(
        "Fetch stage: found=%d kept=%d channels_failed=%d calls=%s",
        r.videos_found,
        r.shorts_kept,
        r.channels_failed,
        guard.calls,
    )
    return r


def _text(conn: sqlite3.Connection, limit: int | None):
    from shorts_digest.pipeline import text_stage

    r = text_stage(conn, limit)
    log.info(
        "Text stage: processed=%d transcript=%d metadata=%d reasons=%s",
        r.processed,
        r.transcripts,
        r.metadata,
        r.errors,
    )
    return r


def _extract(cfg: AppConfig, conn: sqlite3.Connection, limit: int | None):
    from shorts_digest.pipeline import extract_stage

    r = extract_stage(conn, cfg.channels.topic_names, limit)
    log.info(
        "Extract stage: extracted=%d failed=%d usefulness=%s",
        r.processed,
        r.failed,
        dict(sorted(r.by_score.items())),
    )
    return r


def _filter(cfg: AppConfig, conn: sqlite3.Connection):
    from shorts_digest.pipeline import filter_stage

    r = filter_stage(conn, cfg.settings.min_usefulness)
    log.info("Filter stage: accepted=%d rejected=%s", r.accepted, r.rejected)
    return r


def _digest(cfg: AppConfig, conn: sqlite3.Connection, days: int, run_id: int | None):
    """Write output/digest-YYYY-MM-DD.{md,csv}. With a run_id, remember what was sent."""
    from shorts_digest.digest import write_outputs
    from shorts_digest.pipeline import build_digest

    d = build_digest(
        conn,
        cfg.settings.digest_size,
        cfg.settings.max_per_topic,
        days,
        exclude_digested=run_id is not None,
    )
    # A recorded run never replaces an earlier digest from the same day; a rebuild may.
    md_path, csv_path = write_outputs(d, overwrite=run_id is None)
    if run_id is not None:
        store.record_digest(conn, run_id, [(i.video_id, i.rank) for i in d.items])
    log.info(
        "Digest: %d items from %d Shorts -> %s, %s",
        len(d.items),
        d.shorts_considered,
        md_path.relative_to(md_path.parents[1]),
        csv_path.name,
    )
    return d


# --- recorded runs -----------------------------------------------------------------


def summary_line(run_id: int, status: str, s: dict, guard) -> str:
    """The one line CLAUDE.md asks for: found, kept, extracted, rejected, quota."""
    parts = [f"found={s.get('found', 0)}", f"kept={s.get('kept', 0)}"]
    if "text" in s:
        parts.append(f"text={s['text']} (transcript {s['transcripts']})")
    if "extracted" in s:
        parts.append(f"extracted={s['extracted']}")
    if "accepted" in s:
        parts.append(f"accepted={s['accepted']} rejected={s['rejected']}")
    if "digest" in s:
        parts.append(f"digest={s['digest']}")
    if s.get("channels_failed"):
        parts.append(f"channels_failed={s['channels_failed']}")
    parts.append(
        f"quota={guard.units} units ({guard.source} {guard.units}, "
        f"today {guard.used_today()}/{guard.daily_limit})"
    )
    return f"Run {run_id} {status}: " + " ".join(parts)


def _recorded_run(cfg: AppConfig, command: str, limit: int | None) -> int:
    """fetch [-> text -> extract -> filter -> digest], recorded in the runs table."""
    from shorts_digest.pipeline import RUN_DIGEST_WINDOW_DAYS
    from shorts_digest.quota import QuotaLimitError
    from shorts_digest.youtube import YouTubeFatalError

    cfg.require_channels()
    cfg.env.require("YOUTUBE_API_KEY")
    conn = store.connect()
    stale = store.mark_interrupted_runs(conn)
    if stale:
        log.warning("Marked %d earlier run(s) as interrupted; carrying on from there", stale)
    run_id = store.start_run(conn, command)
    guard = _new_guard(cfg, conn)
    s: dict = {}
    stage, complete = "fetch", True
    try:
        f = _fetch(cfg, conn, guard, limit)
        s.update(found=f.videos_found, kept=f.shorts_kept, channels_failed=f.channels_failed)
        complete = f.complete
        if command == "run":
            stage = "text"
            t = _text(conn, limit)
            s.update(text=t.processed, transcripts=t.transcripts)
            stage = "extract"
            e = _extract(cfg, conn, limit)
            s.update(extracted=e.processed, extract_failed=e.failed)
            stage = "filter"
            fl = _filter(cfg, conn)
            s.update(accepted=fl.accepted, rejected=sum(fl.rejected.values()))
            stage = "digest"
            d = _digest(cfg, conn, RUN_DIGEST_WINDOW_DAYS, run_id)
            s["digest"] = len(d.items)
    except Exception as exc:
        # "Fail loud per run": write the failure to the runs table, then exit non-zero.
        store.finish_run(
            conn,
            run_id,
            "failed",
            videos_found=s.get("found", 0),
            shorts_kept=s.get("kept", 0),
            quota_estimate=guard.units,
            error=f"{type(exc).__name__}: {exc}",
            stage=stage,
            summary=s,
        )
        if isinstance(exc, YouTubeFatalError):
            log.error("YouTube API refused the run: %s", exc)
        elif isinstance(exc, QuotaLimitError):
            log.error("Daily quota limit reached: %s", exc)
        else:
            log.exception("Run %d crashed in the %s stage", run_id, stage)
        log.error("%s (stopped in %s stage)", summary_line(run_id, "failed", s, guard), stage)
        return 1

    # A --limit run is "partial": the next run looks back from the last *complete* run.
    status = "success" if complete else "partial"
    store.finish_run(
        conn,
        run_id,
        status,
        videos_found=s["found"],
        shorts_kept=s["kept"],
        quota_estimate=guard.units,
        summary=s,
    )
    log.info("%s", summary_line(run_id, status, s, guard))
    return 0


def _dry_run(cfg: AppConfig) -> int:
    """Print what a run would do. No API calls, no transcripts, no digest, no email."""
    from shorts_digest.pipeline import _since
    from shorts_digest.youtube import MAX_IDS_PER_CALL

    cfg.require_channels()
    conn = store.connect()
    guard = _new_guard(cfg, conn)
    channels = [ch.id for _, ch in cfg.channels.all_channels()]
    cached = store.get_uploads_playlists(conn)
    missing = [c for c in channels if c not in cached]
    since = _since(conn, cfg.settings.lookback_days, datetime.now(UTC))
    # Lower bound: one playlistItems page per channel, one videos.list batch, plus lookups.
    estimate = math.ceil(len(missing) / MAX_IDS_PER_CALL) + len(channels) + 1
    pending = dict(conn.execute("SELECT status, count(*) FROM videos GROUP BY status").fetchall())
    key = "set" if cfg.env.get("YOUTUBE_API_KEY") else "MISSING"
    print(
        "Dry run - nothing is fetched, written or sent.\n"
        f"  channels:        {len(channels)} in {len(cfg.channels.topic_names)} topics "
        f"({len(missing)} need an uploads-playlist lookup)\n"
        f"  looking since:   {since.isoformat(timespec='minutes')}\n"
        f"  quota estimate:  ~{estimate} units (today {guard.used_today()}/{guard.daily_limit} "
        "used)\n"
        f"  stored videos:   {pending or 'none yet'}\n"
        f"  digest:          up to {cfg.settings.digest_size} items, "
        f"max {cfg.settings.max_per_topic} per topic\n"
        f"  email:           {'on' if cfg.settings.email.enabled else 'off'}\n"
        f"  YOUTUBE_API_KEY: {key}"
    )
    return 0


# --- commands ----------------------------------------------------------------------


def _not_implemented(stage: str, phase: int) -> int:
    log.info("'%s' is not implemented yet (planned for phase %d).", stage, phase)
    return 0


def cmd_run(cfg: AppConfig, args: argparse.Namespace) -> int:
    if args.dry_run:
        return _dry_run(cfg)
    return _recorded_run(cfg, "run", args.limit)


def cmd_fetch(cfg: AppConfig, args: argparse.Namespace) -> int:
    return _recorded_run(cfg, "fetch", args.limit)


def cmd_text(cfg: AppConfig, args: argparse.Namespace) -> int:
    _text(store.connect(), args.limit)
    return 0


def cmd_extract(cfg: AppConfig, args: argparse.Namespace) -> int:
    _extract(cfg, store.connect(), args.limit)
    return 0


def cmd_digest(cfg: AppConfig, args: argparse.Namespace) -> int:
    # Filter anything newly extracted, then rebuild from stored data (does not mark items sent).
    conn = store.connect()
    _filter(cfg, conn)
    _digest(cfg, conn, args.days, run_id=None)
    return 0


def cmd_quota(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Today's quota use by source and method, and what each source may still spend."""
    conn = store.connect()
    curated, discovery = _new_guard(cfg, conn), _new_guard(cfg, conn, "discovery")
    rows = store.quota_breakdown(conn, curated.day)
    print(
        f"Quota day {curated.day} (Pacific time): {curated.used_today()} of "
        f"{curated.daily_limit} units used (Google's own limit is 10,000)"
    )
    for r in rows:
        print(f"  {r['source']:<10} {r['method']:<20} {r['calls']:>4} calls {r['units']:>6} units")
    if not rows:
        print("  no calls yet today")
    print(f"Remaining: curated {curated.remaining()}, discovery {discovery.remaining()}")
    return 0


def cmd_resolve_channels(cfg: AppConfig, args: argparse.Namespace) -> int:
    return _not_implemented("resolve-channels", 7)


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
    p.add_argument("--dry-run", action="store_true", help="print the plan; no calls, no email")
    p.add_argument("--limit", type=_positive_int, help="process at most N videos")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("fetch", help="find new uploads + classify Shorts only")
    p.add_argument("--limit", type=_positive_int, help="process at most N videos")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("text", help="transcript (or title + description) for stored Shorts")
    p.add_argument("--limit", type=_positive_int, help="process at most N videos")
    p.set_defaults(func=cmd_text)

    p = sub.add_parser("extract", help="concept extraction (rule-based) on stored Shorts")
    p.add_argument("--limit", type=_positive_int, help="process at most N videos")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("digest", help="rebuild a digest from stored data")
    p.add_argument("--days", type=_positive_int, default=7, help="look back N days (default 7)")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("quota", help="today's YouTube API quota use")
    p.set_defaults(func=cmd_quota)

    p = sub.add_parser("resolve-channels", help="turn channel names into IDs (phase 7)")
    p.add_argument("file", nargs="?", default="config/channel_names.txt")
    p.set_defaults(func=cmd_resolve_channels)

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
