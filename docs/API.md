# API

```
cd backend && pip install -e '.[dev]'
uvicorn roadie.api:app --host 127.0.0.1 --port 8000
```

## Endpoints
| Endpoint | |
|---|---|
| `GET /` | the static frontend, with a strict Content-Security-Policy |
| `GET /api/health` | `{status, live_enabled, live_budget_remaining, gallery_count}`; `gallery_count` is the number of gallery files found (0 means none); `live_budget_remaining` is the smaller of today's and this month's live runs left, or `null` when live is off or has ended |
| `GET /api/gallery` | `[{slug, name, description, built_with, strong_city_count, identified_comics}]` |
| `GET /api/plan/{slug}` | the gallery file; slug must match `^[a-z0-9_]{1,40}$` and exist (422 malformed, 404 unknown) |
| `POST /api/live/search` | `{name}` to candidate matches `[{entity_id, name, description, popularity}]` (live mode only) |
| `POST /api/live/plan` | `{entity_id}` (a UUID from a prior search) to `{job_id, reused, stream}` (live mode only) |
| `GET /api/live/stream/{job_id}` | server-sent events: `started` per Qloo call group, `step` per pipeline step, then `result` (plan and template narration) or `error` |

Gallery requests only read the stored files; they never call OpenAI or Qloo. With no data folder the gallery is empty.

## Environment variables
All optional. The backend reads no other environment value, and never logs or returns one.

| Variable | Default | Meaning |
|---|---|---|
| `ROADIE_DATA_DIR` | `data` (repo root) | private data folder: `fixtures/` and `gallery/` live under it |
| `ROADIE_LIVE` | off | `1` enables live mode |
| `ROADIE_ALLOWED_ORIGINS` | none | comma-separated exact origins allowed by CORS (`*` is ignored) |
| `ROADIE_TRUST_PROXY` | off | take the client IP from `X-Forwarded-For`, counting trusted proxy hops from the right end (set only behind a proxy that appends to that header) |
| `ROADIE_TRUSTED_PROXY_HOPS` | 1 | with `ROADIE_TRUST_PROXY`: how many entries from the right end of `X-Forwarded-For` to take. Entries to the left are client-supplied and never used; a short or malformed header falls back to the direct connection address |
| `ROADIE_LIVE_WORKERS` | 4 | concurrent Qloo calls inside one live run (1 = serial, hard maximum 6); the shared pacer and the call cap still apply |
| `ROADIE_IP_RUNS_PER_HOUR` / `ROADIE_GLOBAL_RUNS_PER_DAY` | 3 / 15 | live plan run limits per IP per hour and globally per day |
| `ROADIE_MONTHLY_RUNS` | 300 | ceiling on live plan runs per calendar month (UTC); kept in `$ROADIE_DATA_DIR/live_budget.json` when that folder exists |
| `ROADIE_MAX_QLOO_PER_SECOND` | 4 | client-side pacing of every Qloo call (the key allows 5; values above 5 are capped, 0 turns pacing off) |
| `ROADIE_LIVE_UNTIL` | none | `YYYY-MM-DD`, the last day live mode runs; afterwards live endpoints answer `503 {"error": "live_ended"}` |
| `ROADIE_IP_SEARCHES_PER_HOUR` / `ROADIE_GLOBAL_SEARCHES_PER_DAY` | 20 / 200 | live search limits |
| `ROADIE_CACHE_SECONDS` | 86400 | the same `entity_id` returns the previous result for this long, with no new Qloo calls |
| `ROADIE_MAX_QLOO_CALLS` | 20 | hard cap of Qloo calls per run, retries included (a run normally makes 14) |
| `ROADIE_MAX_RUNNING_JOBS` | 3 | concurrent live runs; the next gets 429 |
| `ROADIE_LIVE_WORK_DIR` | `$ROADIE_DATA_DIR/live_tmp` | parent of the temporary run folders |
| `OPENAI_API_KEY` | none | optional; used only by the gallery builder's language layer, never by live mode |
| `ROADIE_MODEL`, `ROADIE_REASONING_EFFORT` | `gpt-6-luna`, `low` | language model and reasoning effort for the gallery builder |
| `QLOO_API_KEY` | none | read by the `qloo` CLI at run time only (see [DEPLOY.md](DEPLOY.md)) |

## Live mode
Live mode is optional and off by default. The gallery needs no Qloo credential and no OpenAI key. With `ROADIE_LIVE=1`
and the `qloo` CLI installed and set up on the host, visitors can search a comedian, pick a match, and watch a reduced
run: six cities (`REDUCED_CITIES`), one `where_popular` and one comedy-club places call per city, plus similar comics and
brands. That is about 14 calls and at most 20, retries included.

- Independent calls run on a small worker pool (`ROADIE_LIVE_WORKERS`). Results are read back in a fixed order, so the
  plan does not depend on timing, and a failed call is a warning that does not stop the others.
- Results go to a temporary folder in the fixtures layout, through the same `plan_tour`, are narrated by the template
  (never OpenAI), and the folder is deleted.
- A plan needs a prior search: candidates are remembered server-side and the client sends only an `entity_id`.
- Live plans do no description lookups, so `identified_as_comedian` is null.
- Off means `503 {"error": "live_disabled"}` on every live endpoint.
- Responses and logs carry fixed error codes only: never a key, an environment value, a command line or raw Qloo output.
  Qloo text is length-capped.

## Quota and the end of the key
The organizers set the hackathon key to 10,000 requests per month and 5 per second. Defaults stay well inside that: at
most 15 live runs per day globally, 3 per IP per hour, 300 per month (300 runs x at most 20 calls = 6,000 calls), and a
client-side pace so calls never exceed 4 per second. Every call and retry takes a slot in one shared pace across all
workers, and the call cap is counted under a lock. Cache hits and shared in-flight runs spend no quota; a job expires
after 10 minutes.

The key stays active through Nov 16 and is deactivated after the hackathon closes, so deploy with
`ROADIE_LIVE_UNTIL=2026-11-16`. After that date live endpoints answer `503 {"error": "live_ended"}` with a clear message
that the gallery is still available, `/api/health` reports `live_enabled: false` and `live_budget_remaining: null`, and
the gallery keeps working. If Qloo stops answering earlier, searches and runs fail with the code `qloo_unavailable`,
never a stack trace.

## Measured run time
A full live plan ran against the hackathon server inside the Docker image (limited to 0.5 CPU and 512 MB) and took 133
seconds with the calls run serially. The concurrent worker pool was added afterwards; its effect on a small CPU has not
been measured.
