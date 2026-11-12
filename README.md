# Tech Shorts Digest

Short-form video feeds are built to keep you scrolling. This tool does the scrolling for you.
It watches a hand-picked list of YouTube channels, finds their new Shorts, pulls out the concept
each one teaches with a transparent rule-based extractor (no LLM, no AI API keys), filters out ads
and fluff, and produces a 5–10 item "what to learn next" digest with links.

> **Status:** Phase 10 of 11 — runs daily on GitHub Actions and emails the digest: curated
> channels, "New voices" from small rising creators, and channel suggestions you approve from
> the CLI. Next: an evaluation against hand-scored Shorts.
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
python -m shorts_digest run --dry-run        # print the plan: no API calls, nothing written
python -m shorts_digest run --limit 5        # process at most 5 new videos
python -m shorts_digest fetch                # find new uploads + classify Shorts only
python -m shorts_digest text                 # transcripts for stored Shorts
python -m shorts_digest extract              # concept extraction on stored Shorts
python -m shorts_digest digest --days 7      # rebuild a digest from stored data
python -m shorts_digest quota                # today's API quota use, by source and method
python -m shorts_digest email-test           # send the newest digest to DIGEST_TO
python -m shorts_digest resolve-channels     # add channels by name (see below)
python -m shorts_digest run --no-discovery   # curated channels only
python -m shorts_digest discover --dry-run --max-searches 3   # preview discovery (see below)
python -m shorts_digest channels review      # discovered channels suggested for your list
python -m shorts_digest channels approve <id> --topic <topic>   # shows the diff first
python -m shorts_digest channels reject <id> # never suggest again
python -m shorts_digest channels block <id>  # also drop its Shorts from discovery

pytest -q
ruff check . && ruff format .
```

`run` = fetch → discover → text → extract → filter → digest, and ends with one summary line:

```
Run 5 success: found=0 kept=0 discovered=0 (searches 3) text=0 (transcript 0) extracted=0 accepted=0 rejected=0 digest=0 (+0 new voices) quota=334 units (curated 29, discovery 305, today 1611/6000)
```

## Quota

A daily run costs about **30 of 10,000** YouTube API units: one `playlistItems.list` per channel
plus one `videos.list` per 50 new videos. Each channel's uploads playlist ID is looked up once
and cached in SQLite. `search.list` (100 units) is never used by the curated pipeline.

Every API call goes through one **quota guard** (`quota.py`):

- it knows each method's cost and keeps a per-day ledger in the `quota_usage` table, so several
  runs on one day add up (the day follows Pacific time, when Google resets the quota);
- it refuses a call *before* sending it if today's total would pass `quota.daily_limit`
  (6,000 by default, well under Google's 10,000), so a bug can't burn the whole day;
- `quota.curated_reserve` (500) is kept back for the curated fetch: the planned discovery job
  can only spend what is left.

## Reliability

- **Retries:** HTTP 429/5xx, "rate limit" answers and network drops are retried up to 3 times
  (2 s, then 4 s pauses), and each attempt counts against quota. A bad key or exhausted quota
  stops the run at once. A transcript network error is retried once, then falls back.
- **Fail soft per item, loud per run:** one broken channel or video is logged and skipped. A crash
  in any stage is written to the `runs` row (`status = failed`, `stage`, `error`) and the
  command exits non-zero.
- **Resume:** every video's progress is saved as it moves `new → text_ready → extracted →
  accepted/rejected`, so the next run simply carries on. A run killed mid-way (Ctrl-C, a cancelled
  CI job) is left as `running`; the next run marks it `failed: interrupted`.
- **No overwrites:** a second run on the same day writes `digest-YYYY-MM-DD-2.md` instead of
  replacing the digest already produced.

## Email

With `email.enabled: true` (settings.yaml), `run` emails the digest after writing it: the
Markdown as plain text plus a simple HTML version, with the CSV attached. Days where nothing
passed the filters send nothing (`send_when_empty: false`).

Set in `.env` (locally) or as repository secrets (GitHub): `SMTP_HOST`, `SMTP_PORT`,
`SMTP_USER`, `SMTP_APP_PASSWORD`, `DIGEST_TO`. For Gmail: `smtp.gmail.com`, port `587`, and an
**app password** (Google account → Security → 2-Step Verification → App passwords), never
your normal password. Port 465 uses SSL, anything else STARTTLS. Check the setup with
`python -m shorts_digest email-test`.

`run` checks the SMTP keys *before* doing any work. If sending fails, the run is marked
`failed` (stage `notify`) and its items are un-marked as sent, so the next run sends them.

## Running daily on GitHub Actions

`.github/workflows/daily.yml` runs at 01:30 UTC (07:00 IST) and from the **Run workflow**
button. Steps: install → restore the database → `python -m shorts_digest run` → save the
database → upload `output/` (30 days) and a database backup (14 days) as artifacts.

**Where the database lives.** Runners start empty every day, so `data/digest.db` is kept on
a separate `data` branch that holds only that file. Each run restores it and force-pushes it
back as a single commit: the branch never piles up a history of binary snapshots (the
artifact backup covers "undo"). The save and upload steps run even when the pipeline fails,
so the error recorded in the `runs` table is kept; the job still turns red, and GitHub emails
you about failed scheduled runs.

**Setup:** push the repo, then add the six secrets under Settings → Secrets and variables →
Actions: `YOUTUBE_API_KEY` and the five SMTP ones above. Start one run by hand from the
Actions tab to check it. Expect transcripts to be blocked on GitHub's servers (YouTube often
blocks cloud IPs), so CI digests are mostly judged from titles and descriptions.

## Adding channels

Put names, @handles, channel URLs or IDs in `config/channel_names.txt` (one per line), then run
`python -m shorts_digest resolve-channels [--topic python]` in a terminal. For each line it:

1. looks the channel up by ID or @handle for 1 quota unit; for a plain name it guesses the
   handle ("Tech With Tim" → `@TechWithTim`),
2. shows the match with subscriber count, video count and link, and asks if it is right.
   A guessed handle that looks wrong (no videos, under 1,000 subscribers, or a different
   title) gets a look-alike warning: handles are first-come-first-served, so `@CoreySchafer`
   is a 68-subscriber squatter while the real channel is `@coreyms`,
3. offers a channel search only if you say yes (100 units, at most 10 per run),
4. asks for a topic, skips channels already listed.

At the end it shows the `channels.yaml` diff and writes only after a final yes. The file is
edited as text, so comments and layout survive, and the result is validated before saving.
Quota is logged under the `resolve` source.

## Discovery (small, rising creators)

Curated channels are big names. Discovery looks for small creators whose Shorts are doing
unusually well for their size, using a capped keyword search (`discovery:` in settings.yaml):

1. **Search:** one `search.list` per keyword (100 units each, at most 15 a day, shared across
   all runs that day). Keywords rotate round-robin across days; `order` alternates between
   `viewCount` and `date` and flips daily, so each keyword sees both. One page, no paging.
2. **Enrich:** `videos.list` and `channels.list` in batches of 50 for views, duration,
   subscribers and uploads. Channel stats are cached for 7 days.
3. **Filter:** every candidate is kept or gets a reason: `not_short`, `curated_channel`,
   `channel_rejected` / `channel_blocked`, `wrong_language` (English + Hindi/Hinglish by
   default: YouTube's language tag, else the title's script), `off_topic` (no tech term from
   `config/terms.yaml` in the title or description), `subscribers_hidden`, `too_few_subscribers` /
   `too_many_subscribers` (window 1,000–100,000), `too_few_uploads` (< 5), `too_few_views`
   (< 5,000), `low_views_per_subscriber` (< 2.0), `likely_repost` (same title as an earlier
   video from another channel; the earliest copy survives).
4. **Rank:** `outlier_score = views / subscribers`; one video per channel per day; at most 20.

Kept candidates join the normal pipeline (`videos.source = 'discovery'`): transcript,
extraction and filter. Extraction also judges **style** (`serious` / `funny` / `skit`, from
phrases and hashtags in `config/terms.yaml`) and **has_takeaway**: humour is welcome, but if
the joke is the whole content the Short is rejected as `no_takeaway`. For skits the concept
names what is being joked about (e.g. "Debugging: Why fix the bug when you can hide it").

The digest then has up to three sections (empty ones are left out):

- **main** — curated channels, 8 items, at most 3 per topic;
- **New voices** — up to 3 accepted discovery items, one per channel, with subscriber count,
  views, the keyword that found them and a `[funny]` / `[skit]` tag; they never take main slots;
- **Channels to consider adding** — a discovered channel becomes `suggested` after 3 accepted
  Shorts in 30 days, and is listed (at most once a week) with the exact approve command.

Only you promote channels: `channels approve` shows the `channels.yaml` diff and writes after
a yes; `reject` means it is never suggested again; `block` also drops it from discovery and
withdraws its unsent Shorts. Discovery failing never stops the curated run (the summary line
shows `discovery=FAILED`), and `run --no-discovery` skips it.

`discover --dry-run` makes the real calls (so it spends real quota and counts toward the
daily search cap) but saves nothing and leaves the keyword rotation alone. Both modes print a
report and write every candidate with its numbers and reason to
`output/discovery-YYYY-MM-DD[-dryrun].csv`. Discovery failing never affects the curated run.

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

## Filtering and the digest

- **Filter**: items below `min_usefulness` (3), promotional items, and repeats of a concept
  accepted in the last 30 days are rejected with a reason (`low_usefulness`, `promotional`,
  `duplicate_concept`). Repeats are found with `difflib`: similar characters *and* mostly the
  same words, so "Python decorators" and "Python generators" stay distinct.
- **Rank**: usefulness, then confidence, then items with a real transcript, then newest.
- **Select**: up to `digest_size` (8) items, at most `max_per_topic` (3) per topic.
- **Never twice**: `run` records what it sent in `digest_items`; `digest --days N` rebuilds a
  digest from stored data without marking anything as sent.

Example item from the first real digest (12 Nov 2026, 8 picked from 23 Shorts across 10 channels):

```markdown
## Productivity
1. **5 Excel Keyboard Shortcuts Every User Should Know** — Alt H O I to autofit your columns.
   Tool/method: excel, Alt + equals · Level: intermediate · Score: 5/5
   Next step: Search: "excel autofit tutorial"
   https://www.youtube.com/shorts/f4HJf7w2ci4 — Excel Dictionary
```

## Roadmap

1. ✅ Skeleton — layout, config validation, CLI, tests
2. ✅ Discovery — channels → new videos → Shorts, stored in SQLite
3. ✅ Transcripts with fallback to title + description
4. ✅ Concept extraction (rule-based, no LLM)
5. ✅ Dedup, ranking, Markdown/CSV digest
6. ✅ Quota guard + reliability — retries, resume, run summary, `quota` command
7. ✅ `resolve-channels` — channel names → IDs, confirmed one by one
8. ✅ Discovery part 1 — capped keyword search for small, rising creators (dry-run first)
9. ✅ Discovery part 2 — "New voices" digest section, channel promotion flow
10. ✅ Daily GitHub Actions run + email
11. Evaluation against 30 hand-scored Shorts

## Known limitations

- A Short teaches the *name* of a concept, not the skill. The digest is a "what to learn next" list.
- Many Shorts are visual with little speech, so some will have no usable text.
- Summaries of short transcripts can be wrong; every item keeps its link so you can check.
- Transcript fetching uses an unofficial library and may stop working.
- Extraction is rule-based: strong at spotting known tools and how-to content, weaker at
  summarising. A summary is one transcript sentence, and new terms must be added to
  `config/terms.yaml`.
