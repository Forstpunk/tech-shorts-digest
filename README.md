# Tech Shorts Digest

Short-form video feeds are built to keep you scrolling. This tool does the scrolling for you.
It watches a hand-picked list of YouTube channels, finds their new Shorts, uses an LLM to pull out
the one concept each Short teaches, filters out ads and fluff, and produces a 5–10 item
"what to learn next" digest with links.

> **Status:** Phase 2 of 8 — discovery works: new uploads from 29 channels are classified as
> Shorts and stored in SQLite.
> See [Roadmap](#roadmap).

## How it works (planned pipeline)

```
channels.yaml ──► YouTube Data API ──► keep Shorts (≤180 s) ──► transcript / description
                                                                        │
       digest.md + .csv ◄── rank & balance topics ◄── filter + dedup ◄── LLM extraction
              │                                                            (Gemini → Groq)
              └──► optional email          all state in SQLite (data/digest.db)
```

## Setup

Requires Python 3.11+ (3.12 recommended; very new Python versions may lack wheels for some deps).

```bash
python3.12 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # then fill in your keys — never commit .env
```

Add channels to `config/channels.yaml` (real channel IDs start with `UC`). Tune thresholds in
`config/settings.yaml`.

## Commands

```bash
python -m shorts_digest run                  # full pipeline
python -m shorts_digest run --dry-run        # no LLM calls, no email
python -m shorts_digest fetch                # discovery + classify only
python -m shorts_digest extract              # LLM stage on stored videos
python -m shorts_digest digest --days 7      # rebuild a digest from stored data
python -m shorts_digest discover-channels "data engineering"

pytest -q
ruff check . && ruff format .
```

`fetch` (and `run`, which currently stops after fetch) are live; the other commands are stubs.

## Quota

A daily run costs about **30 of 10,000** YouTube API units: one `playlistItems.list` per channel
plus one `videos.list` per 50 new videos. Each channel's uploads playlist ID is looked up once
and cached in SQLite. `search.list` (100 units) is never used in the daily run. Every run logs
its estimate and stores it in the `runs` table.

## Data model

SQLite at `data/digest.db` (gitignored). Every discovered video is stored, including rejected
ones (`status = rejected`, `reject_reason = not_short`), so nothing is fetched twice. A `--limit`
run is recorded as `partial`, and the next run looks back from the last *complete* run, so
skipped videos are not lost.

## Roadmap

1. ✅ Skeleton — layout, config validation, CLI, tests
2. ✅ Discovery — channels → new videos → Shorts, stored in SQLite
3. Transcripts with fallback to title + description
4. LLM concept extraction
5. Dedup, ranking, Markdown/CSV digest
6. Reliability — retries, fallback provider, resume, quota logging
7. Daily GitHub Actions run + email
8. Evaluation against 30 hand-scored Shorts

## Known limitations

- A Short teaches the *name* of a concept, not the skill. The digest is a "what to learn next" list.
- Many Shorts are visual with little speech, so some will have no usable text.
- Summaries of short transcripts can be wrong; every item keeps its link so you can check.
- Transcript fetching uses an unofficial library and may stop working.
- Free-tier LLM limits change; the pipeline throttles and can spread a backlog over several runs.
