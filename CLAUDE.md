# Roadie: AI tour planner for touring stand-up comedians

Hackathon: Qloo Agentic Hackathon (deadline Oct 31, 2026, 9:45am GMT+6).
Goal: an agent that turns a comedian name into a Qloo-grounded tour plan:
ranked cities, a suggested route, comics to bill with, comedy venues per city, and sponsor candidates.
If the app would work the same without Qloo, it is wrong.

## Hard rules
- Zero spend. No paid services. Language layer uses GPT-6 Luna on a capped
  OpenAI project key, with a template fallback if the key or credit is missing.
- Never write, print, or commit any API key (Qloo or OpenAI). Keys come only
  from environment variables on the backend. Never put keys in client code.
- Cloud sessions have NO Qloo credential and NO real Qloo data. Develop and test against the synthetic
  fixtures in tests/synthetic/. Live Qloo calls and real captures are run only by the owner on his own machine.
- Repo is MIT licensed (private for now; the final submission is a fresh public repo). Keep the README accurate.
- Qloo response data is private and must NEVER be committed (hackathon organizers: storing Qloo data in a
  public repo is not allowed; private server-side caching and a public demo backend making live calls are
  allowed). Real fixtures and the gallery live only under ROADIE_DATA_DIR (default ./data, git-ignored; owner
  backup in ~/roadie-data). Do not add files under data/, fixtures/ or gallery/, do not paste real ids,
  names-with-values or responses into code, tests or docs, and do not rewrite history unless asked.
  backend/tests/test_data_guards.py fails on tracked data files and on uppercase UUIDs outside the synthetic
  allowlist (00000000-0000-4000-8000-<number>).
- The public submission is a fresh repo with code and synthetic test data only.

## Qloo facts (verified on the hackathon server)
- Hackathon base URL is https://hackathon.api.qloo.com. The harness must trust
  it: qloo config set base-url https://hackathon.api.qloo.com
- Backend calls Qloo through the harness CLI: qloo exec <operation> --input '<json>'
  Operations: recommend, rank, describe, where_popular, compare_audiences,
  entity_tags, audience_demographics, trends, find_tags. Output is one JSON object.
- Comedians are person entities: search and similar-comics calls use `--type person`.
- Entity names can be ambiguous (status "needs_input"). Resolve a name to an
  entity ID once with: qloo api search --query "<name>" --type person --take 3 --json
  then pass the ID, not the name, to later calls.
- where_popular returns map cells (geohash plus lat/lng), not city names, and a
  national top 10 is unreliable for smaller acts (affinity saturates near 1.0
  in sparse areas). Instead score NAMED candidate cities one by one with
  within="City, ST", and rank cities ourselves across several signals.
- Convert cells to nearby cities with an offline geocoding step.
- Each live call takes about 7 seconds. Cache every result and ship a
  pre-computed demo gallery as JSON.
- qloo api insights --type person --signal-entities <id> --take 10 returns similar comics
  with affinity and popularity (use popularity to tell peers from bigger or smaller acts).
  Always pass the resolved entity ID, never --signal-query with a name: a name can
  resolve to the wrong entity.
- Comedy venues: qloo api insights --type place --signal-entities <id> --filter-location "City, ST"
  --filter-tags urn:tag:category:place:comedy_club --take 10. The comedy_club tag id is
  urn:tag:category:place:comedy_club. Results mix stand-up, improv and sketch rooms and some
  general venues, and several cities return fewer than 5.
- Brands: qloo api insights --type brand --signal-entities <id> --take 10 returns brands with
  audience overlap (mostly menswear, wellness, lifestyle). Overlap, not sponsorship intent.
- Do not use the legacy recommendations endpoints. insights is a GET request.

## Architecture
- backend/: Python. Agent loop: resolve the comedian, gather signals (fixtures now, the harness
  later), rank, then assemble the plan. Each step is emitted as an ordered event for the UI.
  The FastAPI app is backend/roadie/api.py (see "API" below).
- All fixture and gallery paths go through backend/roadie/paths.py (ROADIE_DATA_DIR, slug regex, no path
  escape); FixtureClient(slug, root=None) defaults to <ROADIE_DATA_DIR>/fixtures; tests pass tests/synthetic.
- plan_tour(slug, client) returns: comedian, cities (ranked), route_suggestion (nearest-neighbor over
  the top 6, Roadie's interpretation), comics_to_bill (peer, bigger act or smaller act, popularity
  band 0.05), comedy_venues (top 6 cities, limited-data note under 5 venues), sponsor_candidates.
- Language layer: backend/roadie/narrator.py. Narrator interface with TemplateNarrator and OpenAINarrator
  (default GPT-6 Luna). narrate(plan) returns {summary, booking_pitch, city_pitches, source, rejected_reason};
  city_pitches is one {city, text} per route city. verify_narration rejects names not in the plan, comics not marked
  identified_as_comedian, more than four brands in the summary or in the booking pitch, the word "eligible" (the
  prompt says "identified comedian"), audience-identity words outside plan names, money/dates/capacity and cause or
  sponsorship-intent claims; any failure (including empty_reply, truncated, json_parse) falls back to the template
  (source template_fallback). The OpenAI request sends max_completion_tokens (2000), never max_tokens. Env vars:
  OPENAI_API_KEY (optional, backend only), ROADIE_MODEL (default gpt-6-luna) and ROADIE_REASONING_EFFORT (defaults to low in code,
  overridable). Tests never touch the network.
- frontend/index.html: one static file (inline CSS and JS, no framework, no build, same-origin requests only), served by
  the API at GET / with a strict CSP (default-src 'self'; style-src/script-src 'self' 'unsafe-inline'; connect-src
  'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'). Every API string goes in via textContent or text
  nodes, never innerHTML (backend/tests/test_frontend.py scans for it); attributes are never built from API strings.
  Colors and fonts are CSS variables at the top. Cold start: after 4 s show "Waking the server", retry /api/health up
  to 90 s. Run it locally: ROADIE_DATA_DIR=/tmp/roadie-demo python scripts/build_gallery.py --fixtures tests/synthetic
  --template, then uvicorn roadie.api:app from backend/ (see README "Frontend").
- tests/synthetic/: invented fixtures (three fictional comedians, same layout as a real folder) used by every
  test. Real fixtures and gallery are private data under ROADIE_DATA_DIR/fixtures and /gallery (see "Private data").
- Deploy on a free host. The harness needs Node 22.19+, so use a Docker image: Dockerfile (python 3.12 slim + node 22,
  @qloo/qloo-harness pinned at 0.1.26, non-root, one uvicorn worker on $PORT, python HEALTHCHECK), .dockerignore,
  render.yaml (free Docker web service; QLOO_API_KEY is sync: false) and docs/DEPLOY.md. Never add data, fixtures,
  gallery, tests or a key to the image or to those files (backend/tests/test_deploy.py checks).
- scripts/docker-entrypoint.sh copies <slug>.json files (^[a-z0-9_]{1,40}\.json$ only, copy not symlink) from
  ROADIE_SECRETS_DIR (default /etc/secrets, an assumption about Render Secret Files) into ROADIE_DATA_DIR/gallery,
  prints counts only (and tells apart a missing, empty and unreadable secrets folder; unreadable means it exists but this user cannot list it, and the app still starts), exports QLOO_BASE_URL and QLOO_TRUSTED_BASE_URL (default the hackathon URL), runs
  `qloo config set base-url` without the key in its environment, then starts uvicorn. The key comes only from the
  QLOO_API_KEY environment variable. /api/health also returns gallery_count (0 = secret files not found; the page
  then says the gallery is unavailable). The free instance is ephemeral: live_budget.json and the cache reset on restart. The hosted demo runs with ROADIE_LIVE=1, ROADIE_QLOO_MODE=persistent and ROADIE_LIVE_UNTIL=2026-11-16 (render.yaml), next to the gallery (see Known limits).

## Persistent harness (backend/roadie/mcp_client.py)
- PersistentHarness owns ONE `qloo mcp` child (usual environment: QLOO_API_KEY, QLOO_BASE_URL, QLOO_TRUSTED_BASE_URL; stderr discarded). Messages
  are one JSON object per line. Order: `initialize` (protocolVersion 2024-11-05, wait for the same id, 120 s allowed, about 40 s on a slow host), the
  `notifications/initialized` notification, then `tools/list` (10 tools, needs qloo_where_popular and qloo_recommend). Calls are `tools/call`; the
  result's content[].text holds a JSON document; result.isError true is a fixed code (harness_tool_error), never the raw message. Tools:
  qloo_where_popular (entity, within); qloo_recommend (signals = ARRAY of entity ids, target_type person|place|brand, filter_location,
  include_tags, limit); qloo_find_tags (tags only).
- Lock around writes, reader thread matching responses to ids, in-flight limit 1 (serialized; matching by id still handles out-of-order answers),
  call timeout 60 s, start-up 120 s, a timeout is retried once through the budget hook (charged to the 20-call cap and the 4/s pacer), two timeouts
  in a row restart the child. A supervisor restarts a dead or wedged child at most 3 times (backoff 1, 2, 4 s; a good answer resets the count), then status
  `unavailable`. It never raises into the server and never touches the gallery.
- McpClient maps documents to the one-off client's shapes (where_popular: operation, status, interpretation.within = the requested city, results
  cells with query.affinity and query.popularity; entities: name, entity_id, popularity, query.affinity; people never keep tags). A document missing a
  needed number fails the call (harness_bad_response); nothing is guessed. The exact key path of affinity in real MCP documents is not verified
  (the code reads query.affinity, and a flat `affinity`). search_person stays on the one-off LiveClient (90 s timeout, no retry) because no
  person-search tool is verified in tools/list.
- Settings: ROADIE_QLOO_MODE = persistent (default) or oneshot (old LiveClient per call). /api/health adds live_ready and live_status
  (ready, starting, unavailable). While starting, search and plan answer 503 {error: live_starting}; after giving up 503 {error: qloo_unavailable};
  streams of running jobs are not gated. The page shows "Live search is warming up, this can take about a minute" and polls /api/health up to 3 minutes.
- Tests use a scripted fake child (backend/tests/fake_mcp.py): no process, no network.

## API (backend/roadie/api.py, live.py, settings.py)
- create_app(settings, client_factory, clock, now, pacer) so tests inject everything. Settings.from_env reads only
  ROADIE_DATA_DIR, ROADIE_LIVE, ROADIE_QLOO_MODE, ROADIE_ALLOWED_ORIGINS, ROADIE_TRUST_PROXY, ROADIE_TRUSTED_PROXY_HOPS, ROADIE_LIVE_WORKERS, ROADIE_LIVE_UNTIL and the limit
  variables (see README). Never read, log or return any other environment value. ROADIE_GALLERY_DIR is gone.
- Gallery: scripts/build_gallery.py writes <ROADIE_DATA_DIR>/gallery/<slug>.json (plan + narration + built_with). The owner
  builds the openai versions locally; the script refuses to replace an openai file with a template one
  without --force. GET /api/gallery and /api/plan/{slug} only serve those files (slug regex
  ^[a-z0-9_]{1,40}$ and must exist; paths come from a directory listing, never from input). Never call
  OpenAI or Qloo on a gallery request.
- Live mode is OPTIONAL and OFF by default (ROADIE_LIVE=1 plus the qloo CLI on the host). Off means 503
  {error: live_disabled} on every live endpoint; past ROADIE_LIVE_UNTIL (the key is active through Nov 16 and
  deactivated after the hackathon) it is 503 {error: live_ended} with a clear message, and the gallery keeps
  working. /api/health adds live_budget_remaining (min of today's and this month's runs left; null when off). Search is one Qloo call; plan is a reduced run (REDUCED_CITIES,
  14 calls, hard cap 20 including retries, run on a worker pool: ROADIE_LIVE_WORKERS default 4, hard max 6, 1 = serial; results are read back in a fixed order so plans never depend on timing; a failed call is a warning, the cap counter is lock-protected and the shared pacer covers all workers) written to a temp folder (under ROADIE_DATA_DIR/live_tmp unless ROADIE_LIVE_WORK_DIR) in the fixtures layout, planned with
  plan_tour, narrated with TemplateNarrator only, folder deleted. Plan requires a prior search (candidates
  are remembered server-side; the client sends only an entity_id). No description lookups, so
  identified_as_comedian is null in live plans.
- Limits (env-configurable; the hackathon key allows 10,000 requests a month and 5 per second): 3 runs per IP
  per hour, 15 per day globally, 300 per calendar month (ROADIE_MONTHLY_RUNS, persisted in
  <data>/live_budget.json when that folder exists), hard cap 20 Qloo calls per run retries included, and a
  shared pacer so Qloo calls never exceed 4 per second (ROADIE_MAX_QLOO_PER_SECOND; every retry takes a slot).
  Also 3 running at once (429), 24 hour cache per entity_id (cache hits and shared in-flight runs spend no quota), jobs expire after 10 minutes. Client IP
  is the direct peer unless ROADIE_TRUST_PROXY: then the X-Forwarded-For entry ROADIE_TRUSTED_PROXY_HOPS (default 1) places from the RIGHT end (a trusted proxy appends the address it saw; left-hand entries are client-supplied and never used); a header shorter than the hop count or a malformed entry falls back to the direct peer. The hop count is unverified for Render (see docs/DEPLOY.md).
- Errors are fixed codes with fixed messages. Never put an exception message, a subprocess command line,
  raw Qloo output, a key or an environment value in a response or a log line. Qloo text is length-capped.

## Known limits
- (a) With one call per process (ROADIE_QLOO_MODE=oneshot) a single qloo call took about 47 s at 0.1 CPU, mostly Node start-up, so live
  plans failed on Render's free instance (a plan hit the 20-call cap). A serial one-off run took 133 s in a 0.5 CPU, 512 MB container.
- (b) With the persistent harness (ROADIE_QLOO_MODE=persistent, now the default) one long-running `qloo mcp` process starts once (about 40 to
  45 s alone, about 105 s locally at 0.1 CPU while the app also starts), after which calls took 0.6 to 4 s each.
- (c) At 0.1 CPU in a local container a full live plan took 28.7 s with no warnings and 0 restarts after a 104.6 s warm-up; the search (still
  one-off) took 46.8 s.
- (d) On Render's free instance, once the harness was ready, the search took 16.9 s and a full live plan took 15.4 s, no warnings, six cities.
- (e) The hosted demo runs with ROADIE_LIVE=1, ROADIE_QLOO_MODE=persistent and ROADIE_LIVE_UNTIL=2026-11-16. The stopping rule (live stays off
  unless a full live plan finishes in about two minutes on the free instance) was applied and passed, with the measurements above. When
  /api/health says live_enabled is false the page hides the form and shows a calm notice.
- (f) NOT measured: a cold start of the Render free instance with live on (container wake-up plus harness warm-up), behavior after a harness
  crash on a real host, and sustained load.
- (g) The hackathon key is deactivated after Nov 16, so live mode ends then by design; the gallery keeps working.

## Dropped sections
The plan has no vibe, shared-audience or trends section. They were cut because entity_tags and
compare_audiences returned thin or tiered data for comedians (no such fixtures were captured, on
purpose), and trends was flat in earlier tests. Do not bring them back without new evidence.

## Working style
- One milestone per session, small commits, tests for the ranking logic.
- Cite the Qloo signal behind every recommendation in the output.
- Flag weak evidence (thin data, near-ties) instead of hiding it.

## Event rules (from the kit's SAFE_USE and API_ACCESS docs)
- Qloo results are aggregate affinities, never claims about individuals or
  causes. Write "high audience affinity in X", never "fans in X will buy".
- Label every output line as a Qloo result or as Roadie's own interpretation.
- If a comedian name is ambiguous, ask the user to choose. Never guess.
- Send Qloo only public comedian and city names. No personal data.
- Cache only what is needed. Gallery stays small (about 15 comedians). No bulk
  scraping. Live calls get a per-visitor rate limit and bounded retries.
- Treat model output and Qloo results as untrusted input.

## Verified qloo exec input shapes (a TOOL_INPUT error does not name the bad field)
- where_popular: {"entity":"<id>","within":"City, ST"}
- entity_tags: {"entities":["<id>"]} (a list, not "entity")
- compare_audiences: {"group_a":["<id>"],"group_b":["<id>"]}
- trends: {"entity_type":"<short alias>","entities":["<id>"],"start_date":"2026-01-01","end_date":"YYYY-MM-DD"}
  entity_type must be the short alias, not the URN. Always start at 2026-01-01:
  a July start returned zero points. Points are weekly.
- entity_tags, compare_audiences and trends are not used for comedians (see Dropped sections).
- A fixture folder holds chosen, search, openers (similar comics), descriptions, brands, where_popular_<city>
  and places_<city> for 12 cities (new_york, los_angeles, chicago, boston, philadelphia, washington, atlanta,
  austin, dallas, denver, seattle, san_francisco). Real ones are private; tests/synthetic/ has three invented
  comedians (june_marlowe: mixed tiers, mostly identified comics, 3-venue, 4-venue and no-venue cities;
  harlan_pike: no identified comics; sol_ambrose: two same-name search results plus chosen.json, saturated).

## Signal quality notes (observed, not assumed)
- where_popular affinity saturates near 1.0 in the real captures (city averages within a few hundredths), so
  most cities are near-ties. Flag them; do not present the order as firm.
- Venue lists differ in length (3 to 10 per city) and mix stand-up, improv and sketch rooms plus a
  few general venues. Qloo gives no capacity or booking policy.
- Brand results are menswear, wellness and lifestyle labels: overlap, not sponsor fit.
- compare_audiences and entity_tags were thin or tiered; trends was flat. Rank, not percentile.

## Data handling and safe use (applies to every change)
- Private data: real Qloo responses and the gallery stay under ROADIE_DATA_DIR, never in git. Tests use only
  tests/synthetic/ (invented names, venues, brands and ids) and must not need ROADIE_DATA_DIR to exist.
- Output wording: aggregate affinity only, labeled Qloo result vs Roadie interpretation; weak evidence flagged.
- Send Qloo only public comedian and city names; ambiguous names go to a human; no personal data; no bulk
  scraping; bounded retries; per-visitor rate limits; no key in code, logs, responses or git.
- Model output and Qloo text are untrusted: cap length, verify narration against the plan, fixed error codes.
