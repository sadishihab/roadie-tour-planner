"""The committed synthetic fixture set (tests/synthetic): every name, id and number is invented.

Tests read these instead of real Qloo data, which is private and never committed. They need no
ROADIE_DATA_DIR. Layout is the same as a real comedian folder.

- june_marlowe: strong, moderate and weaker cities, mostly identified comics, a 3-venue city,
  a 4-venue city and a city with no venues
- harlan_pike: no identified comics (a list of non-comedians); only two strong cities
- sol_ambrose: ambiguous search (two results with the same name) resolved by chosen.json;
  near-saturated affinities (nine strong cities)
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SYNTH_DIR = ROOT / "tests" / "synthetic"
JUNE, HARLAN, SOL = "june_marlowe", "harlan_pike", "sol_ambrose"
SLUGS = sorted(p.name for p in SYNTH_DIR.iterdir() if (p / "chosen.json").is_file())
TAG = "urn:tag:category:place:comedy_club"


def chosen(slug: str) -> dict:
    return json.loads((SYNTH_DIR / slug / "chosen.json").read_text(encoding="utf-8"))


def chosen_id(slug: str) -> str:
    return chosen(slug)["entity_id"]
