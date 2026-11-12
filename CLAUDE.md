# CLAUDE.md — Tech Shorts Digest

Guidance for Claude Code when working in this repository. Read this file fully before writing code.

## 1. What this project is

A personal learning tool. It watches a curated list of YouTube channels, finds their new Shorts, extracts the concept each one teaches, filters out noise, and sends the owner a short digest with links.

The owner finds short-form feeds distracting. The point of this program is that the owner never has to scroll: the program reads the feed and the owner reads a 5-10 item digest.

**Owner profile:** intermediate developer, comfortable with Git and basic Python, some JavaScript, moving toward data engineering. Explain non-obvious choices briefly in code comments and commit messages. Prefer simple, readable code over clever code.

**Secondary goal:** this is a portfolio project. The repository should stay clean, documented and demonstrable (README, sample output, tests).

## 2. Scope

### In scope (version 1)

- YouTube Shorts only, from a curated channel list in `config/channels.yaml`
- Official YouTube Data API v3 for discovery and metadata
- Transcript extraction with a fallback to title and description
- LLM extraction of: concept, method or tool shown, topic tag, usefulness score, one-line summary
- Deduplication of videos and of repeated concepts
- Storage in SQLite
- Digest output as Markdown and CSV, plus an optional email
- Daily scheduled run on GitHub Actions

### Out of scope — do not build unless the owner asks

- Instagram Reels, TikTok or any other platform. The official Instagram API does not return reels in hashtag search, and scraping breaks Meta's terms.
- Any scraping of logged-in pages, use of the owner's cookies, or third-party scraper APIs
- Downloading or storing video or audio files
- A web UI or dashboard (possible later phase)
- Keyword search across all of YouTube in the daily run (too expensive in quota, see section 6)

## 3. Tech stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | Type hints everywhere |
| Dependencies | `pip` + `venv`, pinned in `requirements.txt` | Keep the list short |
| YouTube access | `google-api-python-client` | API key only, no OAuth needed for public data |
| Transcripts | `youtube-transcript-api` | Unofficial; must be wrapped and allowed to fail |
| LLM | Gemini API free tier (primary), Groq free tier (fallback) | Model names live in config, never hardcoded |
| Validation | `pydantic` | For config and LLM output |
| Storage | SQLite via the standard library `sqlite3` | Single file `data/digest.db` |
| Config | YAML (`pyyaml`) + `.env` (`python-dotenv`) | |
| Email | `smtplib` with an app password | Optional, behind a flag |
| Scheduling | GitHub Actions cron | Free for public repos |
| Tests | `pytest` | Network calls mocked |
| Lint/format | `ruff` | |

Before using any LLM SDK or model name, check the provider's current documentation. Model names and free-tier limits change often; do not rely on remembered values.

## 4. Repository layout

```
.
├── CLAUDE.md
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── config/
│   ├── channels.yaml        # curated channels, grouped by topic
│   └── settings.yaml        # thresholds, model names, digest size
├── src/shorts_digest/
│   ├── __init__.py
│   ├── cli.py               # entry point: `python -m shorts_digest ...`
│   ├── config.py            # load and validate YAML + env
│   ├── youtube.py           # channel -> uploads playlist -> new videos
│   ├── shorts.py            # decide whether a video is a Short
│   ├── transcripts.py       # fetch transcript, fallback to description
│   ├── llm.py               # provider-agnostic client with fallback + retry
│   ├── extract.py           # prompt + pydantic schema for concept extraction
│   ├── dedupe.py            # video-level and concept-level dedup
│   ├── store.py             # SQLite schema and queries
│   ├── digest.py            # rank, select, render Markdown and CSV
│   └── notify.py            # email sender
├── prompts/
│   └── extract_concept.md   # the extraction prompt, versioned in Git
├── data/                    # gitignored except .gitkeep
├── output/                  # generated digests
├── tests/
└── .github/workflows/daily.yml
```

## 5. Pipeline

Each stage is a separate function that can be run and tested on its own. Stages read from and write to SQLite so a failed run can resume.

1. **Load config.** Validate `channels.yaml` and `settings.yaml` with pydantic. Fail fast with a clear message.
2. **Discover.** For each channel, get the uploads playlist and list items published since the last successful run (default lookback: 3 days on first run).
3. **Classify.** Fetch durations in batches and keep only Shorts (rules in section 6).
4. **Extract text.** Try the transcript. If unavailable, use title + description and mark `text_source = "metadata"`.
5. **Understand.** Send text to the LLM and parse the structured result (schema in section 7).
6. **Filter.** Drop items below `min_usefulness`, items with `is_promotional = true`, and duplicates.
7. **Store.** Upsert everything, including rejected items with a reason, so nothing is processed twice.
8. **Digest.** Pick the top N per run, balanced across topics. Render Markdown and CSV into `output/`.
9. **Notify.** If email is enabled, send the Markdown digest.

## 6. YouTube API rules

The default quota is 10,000 units a day. `search.list` costs 100 units; most read calls cost 1.

- **Never call `search.list` in the daily pipeline.**
- Resolve each channel once with `channels.list` (part `contentDetails`), cache the uploads playlist ID in SQLite.
- List new uploads with `playlistItems.list` (1 unit per page, 50 items).
- Fetch durations with `videos.list` using up to 50 IDs per call (1 unit).
- Log estimated quota used at the end of every run.
- A one-off `discover-channels` CLI command may use `search.list` to help the owner find channels, capped at 10 calls per invocation with a confirmation prompt.

**Detecting Shorts.** The API has no "is Short" flag. Use:

1. Duration of 180 seconds or less (parse the ISO 8601 `duration` field), and
2. Optionally, a lightweight check of `https://www.youtube.com/shorts/<id>`: a Short returns 200, a regular video redirects to `/watch`. Put this behind a setting (`verify_shorts_url`), rate-limit it, and treat failures as "unknown, fall back to duration only".

**Transcripts.** `youtube-transcript-api` is unofficial and can break or be blocked, particularly from cloud runner IPs. Wrap every call in try/except, never let a transcript failure stop the run, and record the failure reason. Prefer English, then Hindi, then any auto-generated track.

## 7. LLM extraction

The prompt lives in `prompts/extract_concept.md`. Ask for JSON only and validate with this schema:

```python
class ConceptExtraction(BaseModel):
    concept: str            # the main idea, max ~10 words
    method_or_tool: str | None   # technique, library, command or product shown
    topic: str              # must be one of the topics in settings.yaml
    summary: str            # one sentence, plain language
    difficulty: Literal["beginner", "intermediate", "advanced"]
    usefulness: int         # 1-5, see rubric
    is_promotional: bool    # mostly an ad, course pitch or engagement bait
    confidence: Literal["low", "medium", "high"]
    next_step: str | None   # what to search or read to actually learn it
```

**Usefulness rubric (put this in the prompt):**

- 5 — teaches a specific, reusable technique with enough detail to try it
- 4 — names a concrete tool or method and says when to use it
- 3 — a real concept, but only at headline level
- 2 — vague tips, motivation or opinion
- 1 — no learnable content

**Rules:**

- If the text is too thin to judge, return `confidence = "low"` and `usefulness <= 2` rather than guessing.
- The model must not invent details that are not in the text.
- Invalid JSON: retry once with the validation error appended, then mark the item `extraction_failed`.
- Handle 429 responses with exponential backoff, then switch to the fallback provider.
- Throttle requests to stay inside free-tier limits (configurable `requests_per_minute`).
- Cache by video ID: never send the same video to the LLM twice.

## 8. Data model (SQLite)

```sql
channels(channel_id PK, title, topic, uploads_playlist_id, added_at)
videos(video_id PK, channel_id, title, description, published_at,
       duration_seconds, is_short, url, text_source, transcript, fetched_at)
extractions(video_id PK, concept, method_or_tool, topic, summary, difficulty,
            usefulness, is_promotional, confidence, next_step,
            model, prompt_version, created_at)
runs(run_id PK, started_at, finished_at, status, videos_found,
     shorts_kept, quota_estimate, error)
digest_items(run_id, video_id, rank)
```

Schema changes go through a small numbered migration list in `store.py`. Do not drop or recreate tables that hold data.

**Deduplication:**

- Video level: primary key on `video_id`.
- Concept level: normalise `concept` (lowercase, strip punctuation) and compare with concepts from the last 30 days. Start with simple fuzzy matching (`difflib`). Only move to embeddings if the owner asks.

## 9. Configuration

`config/channels.yaml` — the owner maintains this. Never invent channel IDs; if the file is empty, stop and ask.

```yaml
topics:
  ai_llm:
    - id: UCxxxxxxxxxxxxxxxxxxxxxx
      name: Example Channel
  data_engineering: []
  python: []
  cloud_devops: []
```

`config/settings.yaml`:

```yaml
lookback_days: 3
max_short_seconds: 180
verify_shorts_url: false
min_usefulness: 3
digest_size: 8
max_per_topic: 3
llm:
  primary: {provider: gemini, model: "<set from current docs>"}
  fallback: {provider: groq, model: "<set from current docs>"}
  requests_per_minute: 10
email:
  enabled: false
```

`.env` (never commit; keep `.env.example` up to date):

```
YOUTUBE_API_KEY=
GEMINI_API_KEY=
GROQ_API_KEY=
SMTP_HOST=
SMTP_PORT=
SMTP_USER=
SMTP_APP_PASSWORD=
DIGEST_TO=
```

## 10. Commands

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m shorts_digest run                  # full pipeline
python -m shorts_digest run --dry-run        # no LLM calls, no email, prints plan
python -m shorts_digest run --limit 5        # process at most 5 videos
python -m shorts_digest fetch                # discovery + classify only
python -m shorts_digest extract              # LLM stage on stored, unprocessed videos
python -m shorts_digest digest --days 7      # rebuild a digest from stored data
python -m shorts_digest discover-channels "data engineering"   # one-off helper

pytest -q
ruff check . && ruff format .
```

## 11. Digest format

`output/digest-YYYY-MM-DD.md`:

```markdown
# Tech Shorts Digest — 12 Nov 2026
8 picked from 41 new Shorts across 22 channels.

## AI / LLM
1. **<concept>** — <summary>
   Tool/method: <method_or_tool> · Level: <difficulty> · Score: 4/5
   Next step: <next_step>
   <url> — <channel>
```

Also write the same rows to `output/digest-YYYY-MM-DD.csv`. Keep the digest short; a long digest defeats the purpose of the project.

## 12. GitHub Actions

`.github/workflows/daily.yml`:

- Trigger: `schedule` (cron in UTC; 01:30 UTC is 07:00 IST) and `workflow_dispatch`
- Steps: checkout, set up Python, install, run pipeline, upload `output/` as an artifact
- Secrets come from repository secrets, never from files
- Persist `data/digest.db` between runs. Simplest approach: commit it to a separate `data` branch at the end of each run. Explain the approach in the README.
- A failed run should fail the workflow visibly and write the error to the `runs` table

## 13. Build order

Work in these phases. Finish and verify each one before starting the next, and commit at the end of each.

1. **Skeleton** — layout, config loading, `.env.example`, `.gitignore`, CLI stub, one passing test.
2. **Discovery** — channels to new videos to Shorts, stored in SQLite. Verify with `fetch --limit 5` on real channels.
3. **Text** — transcripts with fallback.
4. **Extraction** — prompt, schema, one provider. Save 10 real outputs to `tests/fixtures/` for the owner to review.
5. **Filter and digest** — dedup, ranking, Markdown and CSV.
6. **Reliability** — retries, fallback provider, resume after failure, quota logging.
7. **Automation** — GitHub Actions and email.
8. **Evaluation** — a labelled set of 30 Shorts in `tests/eval/` scored by the owner, and a script that reports how often the model's usefulness score is within 1 of the owner's. Put the result in the README.

## 14. Working rules for Claude Code

- **Plan first.** For any change touching more than one module, state the plan and wait for confirmation.
- **Small steps.** One phase or feature per session. Do not build ahead.
- **Verify before claiming done.** Run the tests and the relevant CLI command, and show the output.
- **No secrets in code, logs, tests or commits.** Never print API keys. Never commit `.env` or `data/`.
- **No new dependencies** without saying why and asking first.
- **Mock the network in tests.** Tests must pass offline and must not spend API quota.
- **Do not fabricate data.** No made-up channel IDs, video IDs, model names or sample extractions presented as real.
- **Fail soft per item, fail loud per run.** One bad video must not stop the pipeline; a broken config or missing key must stop it immediately with a clear message.
- **Logging.** Use the `logging` module, INFO by default, with a per-run summary line: found, kept, extracted, rejected, quota estimate.
- **Commits.** Small, with messages that say what changed and why.
- **When unsure about scope, ask.** Section 2 is the boundary.

## 15. Known limitations to keep visible in the README

- A Short teaches the name of a concept, not the skill. The digest is a "what to learn next" list.
- Many Shorts are visual with little speech, so some will have no usable text.
- Summaries of short transcripts can be wrong; every item keeps its link so the owner can check.
- Transcript fetching is unofficial and may stop working.
- Free-tier LLM limits change; the pipeline throttles and can spread a backlog over several runs.
