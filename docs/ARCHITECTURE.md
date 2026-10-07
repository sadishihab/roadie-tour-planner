# Architecture

## Pipeline (`backend/roadie/`)
The agent loop resolves the comedian, gathers signals, ranks, then assembles the plan. Each step is emitted as an
ordered event for the UI. The planning pipeline itself makes no network call: it reads a fixture folder (saved Qloo
responses, or a temporary folder written by a live run).

- `qloo_client.py`: `LiveClient` runs the `qloo` CLI (timeout, at most 2 retries, entity ids only; ambiguity raises an
  error). `FixtureClient(slug, root=None)` reads `<ROADIE_DATA_DIR>/fixtures/<slug>/`; tests pass `tests/synthetic`.
  Comedians are Qloo person entities. `chosen.json` (entity_id, name, description) records the human's pick; without
  one the pipeline falls back to slug-vs-name matching, which raises when a name is ambiguous.
- `ranking.py` ranks the 12 candidate cities from saved `where_popular` results.
- `pipeline.py`: `plan_tour(slug, client)` returns the plan below plus the ordered step events.
- `cities.py`: candidate cities with approximate city-center coordinates (used for the route).
- `narrator.py`: the language layer. `paths.py`: every data path. `trim.py`: keeps person data minimal.
  `api.py`, `live.py`, `settings.py`: the server (see [API.md](API.md)).

## The plan
| Section | Source | Notes |
|---|---|---|
| `comedian` | Qloo | resolved by entity id |
| `cities` | Qloo + Roadie | tier plus raw `avg_affinity` and `rank`; a plan-level `city_tier_note` says same-tier cities cannot be ordered |
| `route_suggestion` | Roadie | a west-to-east sweep: the strong-tier cities sorted by longitude; not a ranking; ignores travel logistics, dates and routing constraints |
| `comics_to_bill` | Qloo + Roadie | people with overlapping audiences, marked peer, bigger act or smaller act (popularity band 0.05), each with `identified_as_comedian` |
| `comedy_venues` | Qloo | "comedy venues this audience favors in <city>" for each route city; limited-data note under 5 venues |
| `sponsor_candidates` | Qloo | "brands with audience overlap worth approaching"; overlap, not sponsorship intent |

Dropped sections: there is no vibe, shared-audience or trends section. `entity_tags` and `compare_audiences` returned
thin or tiered data for comedians and `trends` was flat, so no such fixtures were captured and the sections were cut.

## Ranking and tiers
Affinity saturates near 1.0 in the real captures (city averages within a few hundredths, most cities within 0.01 of the
top), and average popularity does not track affinity, so a 1 to 12 order overstates the evidence. The plan keeps `rank`
and `avg_affinity` in the data but labels cities with Roadie's own tiers, each relative to the other candidates:
`strong` (within 0.010 of the top city's average affinity), `moderate` (within 0.020), otherwise `weaker`. Cities in the
same tier cannot be meaningfully ordered. The thresholds are constants in `pipeline.py`: a Roadie heuristic, not Qloo
values. The route is a west-to-east sort of the strong cities by longitude and is Roadie's interpretation.

Comics: Qloo returns people with overlapping audiences, not only comedians. `identified_as_comedian` is true if the
person's one-line description contains "comedian" or "comic", false otherwise, and null when no description was
captured. Identified comedians are listed first and only they show the size label. It is a simple text match that can
miss real comics.

Venues: lists differ in length (3 to 10 per city) and mix stand-up, improv and sketch rooms with a few general venues.
Qloo gives affinity, not capacity or booking policy. Brands are mostly menswear, wellness and lifestyle labels:
overlap, not sponsor fit.

## Language layer (`narrator.py`)
`narrate(plan)` returns `{summary, booking_pitch, city_pitches, source, rejected_reason}`; `city_pitches` is one
`{city, text}` per route city.

- `TemplateNarrator` writes everything from the plan alone, with no model.
- `OpenAINarrator` calls the OpenAI chat completions API (default `gpt-6-luna`; one attempt, 20 s timeout, no retries).
  It sends `max_completion_tokens` (2000, because reasoning models count hidden thinking tokens against it) and
  `reasoning_effort` (`low` unless `ROADIE_REASONING_EFFORT` says otherwise). It sends only a compact JSON of the plan,
  never a raw fixture. The prompt limits it to names in the plan and forbids describing the audience's identity.
- `narrate` uses the model when `OPENAI_API_KEY` is set, otherwise the template.
- `verify_narration` rejects model output that names a city, venue, comic or brand not in the plan, names a comic not
  marked `identified_as_comedian`, names more than four brands in the summary or in the booking pitch, uses the word
  "eligible", uses audience-identity words outside a plan name, or contains money, ticket, capacity or date figures,
  cause claims or sponsorship-intent claims. City pitches must cover exactly the route cities. It is a heuristic that
  can miss things and errs toward rejecting.
- Any failure or rejection makes the whole result the template output with `source` `template_fallback`. `source` is
  `openai`, `template` or `template_fallback`; `rejected_reason` is a short code (`timeout`, `network_error`,
  `http_<status>`, `bad_response`, `empty_reply`, `truncated`, `json_parse`, or `rejected: ...`) and never contains
  the key or the request.

## Frontend (`frontend/index.html`)
One static file: inline CSS and JavaScript, no framework, no build, same-origin requests only. The API serves it at
`GET /` with `default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src
'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'`.

- The page shows gallery cards, then the plan view: a west-to-east route strip with no rank numbers, a shared tier
  stated once, per-city sections with a venue-count preview and the limited-data note, comics (only identified comedians
  listed, the rest collapsed), brands labeled "audience overlap, not sponsorship intent", a limits panel and Copy pitch
  buttons.
- "Qloo result" marks only what Qloo returned; peer, bigger act and smaller act use the dashed "Roadie reading" style.
  Internal field names are never shown.
- The optional live search streams steps over EventSource and shows the same plan view with template narration. When `/api/health` says `live_enabled` is false (the case on the hosted demo), the page hides the form and shows one calm notice instead (not the Problem label). A
  sleeping host gets a "Waking the server" notice after 4 s and `/api/health` is retried for up to 90 s.
- Every API string is inserted with `textContent` or text nodes, never `innerHTML` (a test scans for it); attributes are
  never built from API strings. Colors and fonts are CSS variables at the top.
- Tests: `frontend_harness.js` renders the page script against a tiny fake DOM with node (skipped without node), and a
  headless Chromium test (skipped without it) checks 360 and 390 px widths for sideways scroll and 44 px touch targets.

## Known limits
Measured facts about live mode, stated as measured:
- (a) A full live run took 133 seconds with serial calls in a container limited to 0.5 CPU and 512 MB.
- (b) On Render's free instance (0.1 CPU, 512 MB) a single qloo call took about 47 seconds even when run alone,
  apparently mostly Node start-up (a lone search took as long as a lone where_popular). That is longer than the client's
  30 second per-call timeout. A live plan there hit the 20-call safety cap and stopped with a clean message; live search
  (one call) did work.
- (c) Therefore the hosted demo runs with `ROADIE_LIVE=0` and the gallery is the demo. Live mode works on a host with
  enough CPU and is unproven below 0.5 CPU.
- (d) The hackathon key is deactivated after Nov 16, so live mode ends then anyway.

A possible future fix, not yet implemented and untested: keep one long-running harness process (`qloo mcp`) so Node starts once.
