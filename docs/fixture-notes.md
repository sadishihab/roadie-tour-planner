# Fixture notes (layout and signal quality)

Real Qloo responses are private data: they live under `ROADIE_DATA_DIR/fixtures/` (default `data/fixtures/`, git-ignored)
and must never be committed, because the hackathon organizers do not allow Qloo response data in a public repo. This
file therefore describes the layout and the *kinds* of behavior seen, with no real names, ids or values. The
committed test data is the invented set in `tests/synthetic/` (same layout).

## Layout of a comedian folder (`fixtures/<slug>/`)
- `chosen.json`: `{entity_id, name, description}`, the human's pick at capture time. The pipeline resolves by it.
- `search.json`: up to 3 person search hits (`name`, `entity_id`, `popularity`, `properties.short_description`).
  Names can be ambiguous or other, far more popular people can match, so a human confirms the match at capture time.
- `openers.json`: similar people (`insights --type person`), trimmed, ordered by `query.affinity`; the comedian
  itself is not in the list. Qloo returns people with overlapping audiences, not only comedians.
- `descriptions.json`: entity id to one-line description (or null) for each similar person. When the file is
  absent, `identified_as_comedian` is null (unknown) for everyone.
- `brands.json`: brands with audience overlap, trimmed.
- `where_popular_<city>.json` and `places_<city>.json` for the 12 cities in `roadie/cities.py`.
There are no `entity_tags`, `trends` or `compare` files for comedians, on purpose.

## Signal quality (observed on the real captures, no values kept here)
- `where_popular` returns exactly 10 map cells per city (location with geohash, `query.affinity`, `query.popularity`).
  Affinity saturates near 1.0, so most cities sit within a few hundredths of each other and the top group is a set of
  near-ties. Average popularity does not track affinity. That is why the plan uses Roadie's own tiers (strong within
  0.010 of the top city's average affinity, moderate within 0.020, otherwise weaker) rather than a 1 to 12 order. The
  thresholds are a heuristic, not Qloo values.
- The route is the strong-tier cities sorted west to east by longitude. It replaced a nearest-neighbor walk that
  ended with a long final hop; a test checks the sweep is shorter. It ignores travel logistics, dates and routing
  constraints.
- Venue lists (`insights --type place --filter-tags urn:tag:category:place:comedy_club`) range from a few to 10
  per city, so several cities fall under 5 and get a limited-data note. They mix stand-up rooms with improv and
  sketch rooms and a few general venues; Qloo gives no capacity or booking policy.
- Brand results are mostly menswear, wellness and lifestyle labels: audience overlap, not sponsorship intent.
- `entity_tags` and `compare_audiences` returned thin or tiered data for comedians and `trends` was flat, so the
  plan has no vibe, shared-audience or trends section.

## Privacy: no tags on person fixtures
Qloo's tags on person entities are Wikipedia-style categories about named individuals (they can name sexual
orientation, religion or ethnicity), and Qloo's safe-use rules say not to infer or surface sensitive traits.
`roadie.trim` keeps only `name`, `entity_id`, `type`, `popularity`, `query.affinity` and
`properties.short_description` for person entities; venue and brand tags are descriptors and are kept. Tests fail
if a person fixture has a `tags` field or contains sensitive words. Earlier commits in git history still contain
real fixtures (and, before the trim, person tags); history has not been rewritten, which is why the public
submission is a fresh repository.

## Similar people are not all comedians
Many similar people are not stand-up comics (chefs, politicians, historians). `qloo api entity --id <uuid> --json`
returns `properties.short_description`; `LiveClient.person_description` returns only that string and
`scripts/capture_comedian.py` saves `descriptions.json`. `identified_as_comedian` is true when the description
contains "comedian" or "comic" (case-insensitive), false otherwise, null for a person absent from a captured file.
It is a simple text match on a one-line description and can miss real comics; identified comedians are listed first.

## Trimming
`roadie.trim.trim_entity` keeps `name`, `entity_id`, `type`, `subtype`, `popularity`, `query.affinity`,
`properties.short_description` and tag names at their original paths for places and brands.
`scripts/trim_fixture.py <slug>/<file>.json ...` trims in place offline inside the private fixtures folder;
`scripts/capture_comedian.py` saves openers, brands and places trimmed.
