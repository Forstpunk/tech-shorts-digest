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
from pathlib import Path

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
    for cid in r.suggested:
        log.info("Discovered channel %s now suggested; see `channels review`", cid)
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
        store.record_digest(conn, run_id, [(i.video_id, i.rank) for i in d.items + d.new_voices])
        store.mark_suggested_shown(conn, [sg.channel_id for sg in d.suggestions])
    log.info(
        "Digest: %d items + %d new voices + %d channel suggestions from %d Shorts -> %s, %s",
        len(d.items),
        len(d.new_voices),
        len(d.suggestions),
        d.shorts_considered,
        md_path.relative_to(md_path.parents[1]),
        csv_path.name,
    )
    return d, md_path, csv_path


def _notify(cfg: AppConfig, conn: sqlite3.Connection, run_id: int, d, md_path, csv_path) -> str:
    """Email the digest. On failure, un-mark the items as sent (so the next run resends)
    and re-raise, so the run fails visibly. Returns what happened, for the summary."""
    from shorts_digest.notify import digest_subject, send_digest

    if not (d.items or d.new_voices or d.suggestions) and not cfg.settings.email.send_when_empty:
        log.info("Email skipped: nothing passed the filters today")
        return "skipped_empty"
    subject = digest_subject(f"{d.day.day} {d.day:%b %Y}", len(d.items), len(d.new_voices))
    try:
        send_digest(cfg.env, subject, md_path.read_text(encoding="utf-8"), csv_path)
    except Exception:
        store.unrecord_digest(conn, run_id, [sg.channel_id for sg in d.suggestions])
        raise
    log.info("Email sent: %s", subject)
    return "sent"


# --- recorded runs -----------------------------------------------------------------


def summary_line(run_id: int, status: str, s: dict, guard, discovery_guard=None) -> str:
    """The one line CLAUDE.md asks for: found, kept, extracted, rejected, quota."""
    parts = [f"found={s.get('found', 0)}", f"kept={s.get('kept', 0)}"]
    if "discovered" in s:
        parts.append(f"discovered={s['discovered']} (searches {s['searches']})")
    if "discovery_error" in s:
        parts.append("discovery=FAILED")
    if "text" in s:
        parts.append(f"text={s['text']} (transcript {s['transcripts']})")
    if "extracted" in s:
        parts.append(f"extracted={s['extracted']}")
    if "accepted" in s:
        parts.append(f"accepted={s['accepted']} rejected={s['rejected']}")
    if "digest" in s:
        parts.append(f"digest={s['digest']} (+{s.get('new_voices', 0)} new voices)")
    if "email" in s:
        parts.append(f"email={s['email']}")
    if s.get("channels_failed"):
        parts.append(f"channels_failed={s['channels_failed']}")
    d_units = discovery_guard.units if discovery_guard else 0
    split = f"curated {guard.units}" + (f", discovery {d_units}" if discovery_guard else "")
    parts.append(
        f"quota={guard.units + d_units} units ({split}, "
        f"today {guard.used_today()}/{guard.daily_limit})"
    )
    return f"Run {run_id} {status}: " + " ".join(parts)


def _discover_in_run(cfg: AppConfig, conn: sqlite3.Connection, guard, s: dict) -> None:
    """Discovery inside `run`. Never lets a failure stop the curated pipeline."""
    from shorts_digest import digest as digest_mod
    from shorts_digest import youtube
    from shorts_digest.discovery import discover, write_report_csv

    try:
        client = youtube.build_client(cfg.env.get("YOUTUBE_API_KEY"))
        r = discover(cfg, conn, client, guard)
        write_report_csv(r, digest_mod.DEFAULT_OUTPUT_DIR, guard.day)
        s.update(discovered=len(r.kept), searches=r.searches)
        log.info(
            "Discovery stage: searches=%d judged=%d kept=%d %s",
            r.searches,
            len(r.candidates),
            len(r.kept),
            r.drop_counts(),
        )
    except Exception as exc:  # fail soft: log it, record it in the run summary, carry on
        s["discovery_error"] = f"{type(exc).__name__}: {exc}"
        log.warning("Discovery failed, continuing with curated only: %s", exc)


def _recorded_run(cfg: AppConfig, command: str, limit: int | None, discovery: bool = True) -> int:
    """fetch [-> discover -> text -> extract -> filter -> digest], recorded in runs."""
    from shorts_digest.pipeline import RUN_DIGEST_WINDOW_DAYS
    from shorts_digest.quota import QuotaLimitError
    from shorts_digest.youtube import YouTubeFatalError

    cfg.require_channels()
    cfg.env.require("YOUTUBE_API_KEY")
    email = command == "run" and cfg.settings.email.enabled
    if email:  # fail fast: don't spend a whole run and then discover the email can't go out
        from shorts_digest.notify import SMTP_KEYS

        cfg.env.require(*SMTP_KEYS)
    conn = store.connect()
    stale = store.mark_interrupted_runs(conn)
    if stale:
        log.warning("Marked %d earlier run(s) as interrupted; carrying on from there", stale)
    run_id = store.start_run(conn, command)
    guard = _new_guard(cfg, conn)
    d_guard = None
    s: dict = {}
    stage, complete = "fetch", True
    try:
        f = _fetch(cfg, conn, guard, limit)
        s.update(found=f.videos_found, kept=f.shorts_kept, channels_failed=f.channels_failed)
        complete = f.complete
        if command == "run" and discovery and cfg.settings.discovery.enabled:
            stage = "discover"
            d_guard = _new_guard(cfg, conn, "discovery")
            _discover_in_run(cfg, conn, d_guard, s)
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
            d, md_path, csv_path = _digest(cfg, conn, RUN_DIGEST_WINDOW_DAYS, run_id)
            s.update(digest=len(d.items), new_voices=len(d.new_voices))
            if email:
                stage = "notify"
                s["email"] = _notify(cfg, conn, run_id, d, md_path, csv_path)
    except Exception as exc:
        # "Fail loud per run": write the failure to the runs table, then exit non-zero.
        store.finish_run(
            conn,
            run_id,
            "failed",
            videos_found=s.get("found", 0),
            shorts_kept=s.get("kept", 0),
            quota_estimate=guard.units + (d_guard.units if d_guard else 0),
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
        line = summary_line(run_id, "failed", s, guard, d_guard)
        log.error("%s (stopped in %s stage)", line, stage)
        return 1

    # A --limit run is "partial": the next run looks back from the last *complete* run.
    status = "success" if complete else "partial"
    store.finish_run(
        conn,
        run_id,
        status,
        videos_found=s["found"],
        shorts_kept=s["kept"],
        quota_estimate=guard.units + (d_guard.units if d_guard else 0),
        summary=s,
    )
    log.info("%s", summary_line(run_id, status, s, guard, d_guard))
    return 0


def _discovery_plan(cfg: AppConfig, conn: sqlite3.Connection, guard) -> str:
    d = cfg.settings.discovery
    if not d.enabled:
        return "off"
    left = max(d.max_searches_per_day - store.searches_on(conn, guard.day), 0)
    n = min(left, len(d.keywords))
    return f"on, {n} searches left today (~{n * 100 + 2 * math.ceil(n / 2)} units)"


def _email_plan(cfg: AppConfig) -> str:
    from shorts_digest.notify import SMTP_KEYS

    if not cfg.settings.email.enabled:
        return "off"
    missing = [k for k in SMTP_KEYS if not cfg.env.get(k)]
    return f"on, but MISSING {', '.join(missing)}" if missing else "on (SMTP keys set)"


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
        f"  discovery:       {_discovery_plan(cfg, conn, guard)}\n"
        f"  email:           {_email_plan(cfg)}\n"
        f"  YOUTUBE_API_KEY: {key}"
    )
    return 0


# --- commands ----------------------------------------------------------------------


def cmd_run(cfg: AppConfig, args: argparse.Namespace) -> int:
    if args.dry_run:
        return _dry_run(cfg)
    return _recorded_run(cfg, "run", args.limit, discovery=not args.no_discovery)


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


def cmd_email_test(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Send the newest digest in output/ to DIGEST_TO, to check the SMTP setup."""
    from shorts_digest import digest as digest_mod
    from shorts_digest.notify import SMTP_KEYS, send_digest

    cfg.env.require(*SMTP_KEYS)
    files = sorted(
        digest_mod.DEFAULT_OUTPUT_DIR.glob("digest-*.md"), key=lambda p: p.stat().st_mtime
    )
    if not files:
        log.error("No digest in output/ yet. Run `python -m shorts_digest digest` first.")
        return 1
    md_path = files[-1]
    send_digest(
        cfg.env,
        f"Tech Shorts Digest — test email ({md_path.name})",
        md_path.read_text(encoding="utf-8"),
        md_path.with_suffix(".csv"),
    )
    log.info("Test email sent with %s", md_path.name)
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


def cmd_discover(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Discovery job only (search -> enrich -> filter -> rank). Prints a review report."""
    from shorts_digest import digest as digest_mod
    from shorts_digest import youtube
    from shorts_digest.discovery import discover, render_report, write_report_csv
    from shorts_digest.quota import QuotaLimitError

    d = cfg.settings.discovery
    if not d.enabled:
        log.info("Discovery is disabled in settings.yaml (discovery.enabled).")
        return 0
    cfg.env.require("YOUTUBE_API_KEY")
    conn = store.connect()
    guard = _new_guard(cfg, conn, "discovery")
    client = youtube.build_client(cfg.env.get("YOUTUBE_API_KEY"))
    try:
        result = discover(
            cfg, conn, client, guard, dry_run=args.dry_run, max_searches=args.max_searches
        )
    except (youtube.YouTubeFatalError, QuotaLimitError) as exc:
        log.error("Discovery failed: %s", exc)
        return 1
    print(render_report(result, show_dropped=args.show_dropped))
    csv_path = write_report_csv(result, digest_mod.DEFAULT_OUTPUT_DIR, guard.day)
    log.info(
        "Discovery %s: searches=%d found=%d judged=%d kept=%d dropped=%d -> %s "
        "quota=%d units (discovery %d, today %d/%d)",
        "dry run" if args.dry_run else "done",
        result.searches,
        result.hits,
        len(result.candidates),
        len(result.kept),
        len(result.candidates) - len(result.kept),
        csv_path.name,
        guard.units,
        guard.used_today("discovery"),
        guard.used_today(),
        guard.daily_limit,
    )
    return 0


def cmd_channels(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Review and decide on discovered channels (only the owner promotes channels)."""
    from shorts_digest.channels import approve_channel, review_lines, set_owner_status
    from shorts_digest.config import DEFAULT_CONFIG_DIR

    conn = store.connect()
    if args.action == "review":
        print("\n".join(review_lines(store.channel_suggestions(conn))))
        return 0
    if args.action == "approve":
        if not _interactive():
            log.error("channels approve asks for confirmation, so run it in a terminal.")
            return 2
        approve_channel(
            conn,
            DEFAULT_CONFIG_DIR / "channels.yaml",
            args.channel_id,
            args.topic,
            cfg.channels.topic_names,
            _ask,
            print,
        )
        return 0
    set_owner_status(
        conn, args.channel_id, {"reject": "rejected", "block": "blocked"}[args.action], print
    )
    return 0


def _interactive() -> bool:
    import sys

    return sys.stdin.isatty()


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:  # stdin closed: treat as "no"
        return ""


def cmd_resolve_channels(cfg: AppConfig, args: argparse.Namespace) -> int:
    """Names -> verified channel IDs -> channels.yaml, each step confirmed by the owner."""
    from shorts_digest import youtube
    from shorts_digest.channels import Resolver, apply_with_confirmation, read_names
    from shorts_digest.config import DEFAULT_CONFIG_DIR, PROJECT_ROOT
    from shorts_digest.quota import QuotaLimitError

    if not _interactive():  # e.g. GitHub Actions: there is nobody to confirm
        log.error("resolve-channels asks for confirmation, so run it in a terminal.")
        return 2
    if args.topic and args.topic not in cfg.channels.topic_names:
        raise ConfigError(
            f"--topic {args.topic!r} is not a topic in channels.yaml "
            f"({', '.join(cfg.channels.topic_names)})"
        )
    cfg.env.require("YOUTUBE_API_KEY")
    names_path = Path(args.file)
    if not names_path.is_absolute():
        names_path = PROJECT_ROOT / names_path
    entries = read_names(names_path)
    if not entries:
        log.info("%s has no channel names in it.", names_path.name)
        return 0

    conn = store.connect()
    guard = _new_guard(cfg, conn, "resolve")
    client = youtube.build_client(cfg.env.get("YOUTUBE_API_KEY"))
    resolver = Resolver(client, guard, cfg.channels, _ask, print, args.topic)
    try:
        result = resolver.resolve(entries)
    except (QuotaLimitError, youtube.YouTubeFatalError) as exc:
        log.error("Stopped: %s (nothing was written)", exc)
        return 1
    print()
    for raw, why in result.skipped:
        print(f"  skipped {raw!r}: {why}")
    written = apply_with_confirmation(
        DEFAULT_CONFIG_DIR / "channels.yaml", result.added, _ask, print
    )
    log.info(
        "resolve-channels: %d to add, %d skipped, written=%s, searches=%d, quota=%d units",
        len(result.added),
        len(result.skipped),
        written,
        result.searches,
        guard.units,
    )
    return 0


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
    p.add_argument("--no-discovery", action="store_true", help="curated channels only")
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

    p = sub.add_parser("discover", help="find small, rising creators (keyword search)")
    p.add_argument(
        "--dry-run", action="store_true", help="real searches, but save nothing; prints a report"
    )
    p.add_argument("--max-searches", type=_positive_int, help="at most N searches (100 units each)")
    p.add_argument(
        "--show-dropped",
        type=int,
        default=3,
        metavar="N",
        help="examples per drop reason in the report (default 3; full list in the CSV)",
    )
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("channels", help="review discovered channels and approve/reject/block")
    ch_sub = p.add_subparsers(dest="action", required=True)
    ch_sub.add_parser("review", help="list channels suggested for the curated list")
    a = ch_sub.add_parser("approve", help="add a discovered channel to channels.yaml")
    a.add_argument("channel_id")
    a.add_argument("--topic", required=True, help="topic in channels.yaml to add it under")
    for action, text in (
        ("reject", "never suggest this channel again"),
        ("block", "also drop its Shorts from discovery"),
    ):
        ch_sub.add_parser(action, help=text).add_argument("channel_id")
    p.set_defaults(func=cmd_channels)

    p = sub.add_parser("email-test", help="send the newest digest to DIGEST_TO (SMTP check)")
    p.set_defaults(func=cmd_email_test)

    p = sub.add_parser("quota", help="today's YouTube API quota use")
    p.set_defaults(func=cmd_quota)

    p = sub.add_parser(
        "resolve-channels", help="turn channel names into IDs and add them to channels.yaml"
    )
    p.add_argument("file", nargs="?", default="config/channel_names.txt")
    p.add_argument("--topic", help="default topic offered for every channel")
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
