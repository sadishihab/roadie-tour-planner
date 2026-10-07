# Data handling

## Private data
The Qloo hackathon organizers confirmed that storing Qloo response data in a public repository is not allowed. Private
server-side caching, and a public demo backend that makes live Qloo calls, are allowed. So this repository holds code and
synthetic test data only. Real responses ("fixtures") and the demo gallery built from them live in a private folder
outside version control:

```
export ROADIE_DATA_DIR=~/roadie-data     # default: data/ at the repo root (git-ignored)
# $ROADIE_DATA_DIR/fixtures/<slug>/...   saved Qloo responses, one folder per comedian
# $ROADIE_DATA_DIR/gallery/<slug>.json   plan + narration built from them
```

`data/`, `fixtures/` and `gallery/` are in `.gitignore`. All code that reads or writes those files goes through
`backend/roadie/paths.py`: slugs must match `^[a-z0-9_]{1,40}$` and paths cannot escape the folder.

## Guard tests
`backend/tests/test_data_guards.py` fails if any tracked file sits under `data/`, `fixtures/` or `gallery/`, or if any
tracked file contains an uppercase UUID (the form of a real Qloo entity id) that is not a synthetic test id
(`00000000-0000-4000-8000-<number>`). Never commit real data and never copy it into `tests/`, code or docs.

## Synthetic test data
`tests/synthetic/` holds three invented comedians (`june_marlowe`, `harlan_pike`, `sol_ambrose`) with fictional venues,
brands and ids, in the same layout as a real comedian folder. They cover mixed identified and unidentified comics, cities
with few or no venues, saturated affinity, and two same-name search results with a `chosen.json`. Tests need no
`ROADIE_DATA_DIR`, no network and no Qloo access. See [fixture-notes.md](fixture-notes.md) for the folder layout.

## Capture and gallery (owner only)
- `scripts/capture_comedian.py "<name>"` runs on a machine with Qloo access. It asks a human to confirm the match, then
  saves trimmed search, similar-comics, description, brand, `where_popular` and comedy-club results for 12 candidate
  cities under `$ROADIE_DATA_DIR/fixtures/<slug>/`. Existing files are skipped.
- `scripts/build_gallery.py` writes `$ROADIE_DATA_DIR/gallery/<slug>.json` for every fixture folder that has a
  `chosen.json`. Without `OPENAI_API_KEY` it writes template narrations; it refuses to replace an `openai` file with a
  template one unless `--force` is given. The gallery is deployed to the server out of band, never through git.

## Privacy and safe use
Qloo's person tags are Wikipedia-style categories about named individuals and can name sexual orientation, religion or
ethnicity. Qloo's safe-use rules say not to infer or surface sensitive traits, so Roadie never keeps them: `roadie.trim`
keeps only name, entity id, type, popularity, affinity and the one-line description for person entities. Tests fail if
a synthetic person fixture has a `tags` field or sensitive words. Venue and brand tags are descriptors and are kept.

Rules that apply to every change:
- Qloo results are aggregate audience affinities, never claims about individuals or causes. Output says "high audience
  affinity in X". Every output line is labeled as a Qloo result or as Roadie's own interpretation.
- Send Qloo only public comedian and city names; no personal data. Ambiguous names go to a human.
- Cache only what is needed, keep the gallery small (about 15 comedians), no bulk scraping, per-visitor rate limits and
  bounded retries on live calls.
- No API key (Qloo or OpenAI) is ever written, printed or committed. Keys come only from backend environment variables
  or the `qloo` CLI's own setup.
- Model output and Qloo text are untrusted: text is length-capped, narration is verified against the plan, and errors
  carry fixed codes only.
