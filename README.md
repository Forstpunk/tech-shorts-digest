# Tech Shorts Digest

Short-form video feeds are built to keep you scrolling. This tool does the scrolling for you.
It watches a hand-picked list of YouTube channels, finds their new Shorts, pulls out the concept
each one teaches with a transparent rule-based extractor (no LLM, no AI API keys), filters out ads
and fluff, and produces a 5–10 item "what to learn next" digest with links.

> **Status:** Phase 4 of 8 — discovery, transcripts and concept extraction work. The digest
> (filtering, ranking, Markdown/CSV) comes next.
> See [Roadmap](#roadmap).

## How it works (planned pipeline)

```
channels.yaml ──► YouTube Data API ──► keep Shorts (≤180 s) ──► transcript / description
                                                                        │
       digest.md + .csv ◄── rank & balance topics ◄── filter + dedup ◄── rule-based extraction
              │                                                         (config/terms.yaml)
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
python -m shorts_digest run --dry-run        # no email, prints plan
python -m shorts_digest fetch                # discovery + classify only
python -m shorts_digest text                 # transcripts for stored Shorts
python -m shorts_digest extract              # concept extraction on stored Shorts
python -m shorts_digest digest --days 7      # rebuild a digest from stored data
python -m shorts_digest discover-channels "data engineering"

pytest -q
ruff check . && ruff format .
```

`fetch`, `text`, `extract` and `run` (which currently stops after extraction) are live; `digest` and
`discover-channels` are stubs.

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

## Transcripts

Captions come from `youtube-transcript-api` (unofficial). Preference: English, then Hindi, then any
auto-generated track; human captions beat auto-generated ones. If there is no usable transcript
the item keeps going with title + description (`text_source = metadata`) and the reason is stored
in `transcript_error`. After 3 blocked requests in a row the run stops asking and uses metadata
for the rest — YouTube often blocks cloud IPs such as GitHub Actions runners.

First local run: **22 of 23 Shorts had an English transcript**; one had captions disabled.

## Concept extraction (no LLM)

A deliberate choice: no LLM and no AI API keys. `extract.py` reads each Short's text and fills the
same fields an LLM would (concept, tool, topic, summary, difficulty, usefulness 1–5, promo flag,
confidence, next step) using:

- **Vocabulary** from `config/terms.yaml`: tools, concepts and advanced terms per topic
- **Patterns**: keyboard shortcuts (`Ctrl + Shift + L`, "control A") and commands (`pip install x`)
- **Signals**: how-to language (judged as a rate per 100 words), hype, news/opinion and promo phrases
- **Extractive summary**: the transcript sentence with the most matched terms; nothing is invented

Every score is stored with its **reasons**, e.g.
`["mentions: excel, autofit, Alt + equals", "instructional language x6", "specific steps: Alt + equals, Alt H"]`,
so wrong scores can be fixed by editing `config/terms.yaml`. First run on 23 real Shorts:
the two Excel shortcut tutorials scored 5, while robot/air-taxi clips and a career-opinion Short
scored 1. Ten reviewed samples live in `tests/fixtures/extractions_sample.json`.

## Roadmap

1. ✅ Skeleton — layout, config validation, CLI, tests
2. ✅ Discovery — channels → new videos → Shorts, stored in SQLite
3. ✅ Transcripts with fallback to title + description
4. ✅ Concept extraction (rule-based, no LLM)
5. Dedup, ranking, Markdown/CSV digest
6. Reliability — retries, resume, quota logging
7. Daily GitHub Actions run + email
8. Evaluation against 30 hand-scored Shorts

## Known limitations

- A Short teaches the *name* of a concept, not the skill. The digest is a "what to learn next" list.
- Many Shorts are visual with little speech, so some will have no usable text.
- Summaries of short transcripts can be wrong; every item keeps its link so you can check.
- Transcript fetching uses an unofficial library and may stop working.
- Extraction is rule-based: strong at spotting known tools and how-to content, weaker at
  summarising. A summary is one transcript sentence, and new terms must be added to
  `config/terms.yaml`.
