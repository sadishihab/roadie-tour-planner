#!/usr/bin/env python3
"""Capture live Qloo responses for one touring comedian into <data>/fixtures/<slug>/.

<data> is ROADIE_DATA_DIR (default data/ at the repo root, git-ignored). Real Qloo data is private and
must never be committed.

Run by the owner on his own machine (needs the qloo CLI and credentials).
Existing files are skipped, so a capture can be resumed without repeat calls.

    python scripts/capture_comedian.py "Comedian Name"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from roadie.cities import CITIES  # noqa: E402  (the one constant holding the 12 cities)
from roadie.pipeline import COMEDY_CLUB_TAG  # noqa: E402
from roadie.paths import fixture_file, fixture_folder, fixtures_dir  # noqa: E402
from roadie.qloo_client import LiveClient, QlooClient, build_chosen, city_slug, slugify  # noqa: E402
from roadie.trim import trim_payload  # noqa: E402


def _save(path: Path, fetch) -> bool:
    """Write fetch() to path unless it exists. Returns True if a call was made."""
    if path.exists():
        print(f"skip  {path.name}")
        return False
    data = fetch()
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"saved {path.name}")
    return True


def choose(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Show candidates and let the human pick; never guess, even with one result."""
    for i, e in enumerate(candidates, 1):
        desc = (e.get("properties") or {}).get("short_description") or ""
        print(f"  {i}. {e.get('name')}  popularity: {e.get('popularity')}  id: {e.get('entity_id')}  {desc[:80]}")
    answer = input(f"Choose 1-{len(candidates)} (anything else aborts): ").strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(candidates):
        raise SystemExit("aborted: no comedian chosen")
    return candidates[int(answer) - 1]


def capture(client: QlooClient, name: str, out_root: Path | None, cities: list[str] = CITIES) -> Path:
    slug = slugify(name)[:40]
    out = fixture_folder(slug, out_root)  # validated slug, inside the private fixtures folder
    out.mkdir(parents=True, exist_ok=True)

    def path_of(filename: str) -> Path:
        return fixture_file(slug, filename, out_root)

    search_path = path_of("search.json")
    if search_path.exists():
        candidates = json.loads(search_path.read_text(encoding="utf-8"))
    else:
        candidates = client.search_person(name)
    if not candidates:
        raise SystemExit(f"no comedian found for {name!r}")
    print(f"Search results for {name!r}:")
    chosen = choose(candidates)
    _save(search_path, lambda: trim_payload(candidates))
    entity_id = chosen["entity_id"]
    print(f"Chosen: {chosen.get('name')} ({entity_id})")
    # The human's pick, so later runs resolve the comedian by ID (names can be ambiguous). No tags.
    _save(path_of("chosen.json"), lambda: build_chosen(candidates, entity_id))

    # Similar comics and brands are saved trimmed (raw entities are about 30 KB each). Person
    # entities (search, openers) are saved without tags: those can name sensitive traits.
    _save(path_of("openers.json"), lambda: trim_payload(client.similar(entity_id)))
    # Qloo returns people with overlapping audiences, not only comedians, and the saved openers carry
    # no description. One entity lookup per person saves its one-line description (never tags).
    openers = json.loads((path_of("openers.json")).read_text(encoding="utf-8"))
    _save(
        path_of("descriptions.json"),
        lambda: {p["entity_id"]: client.person_description(p["entity_id"]) for p in openers},
    )
    _save(path_of("brands.json"), lambda: trim_payload(client.brands(entity_id)))
    for city in cities:
        _save(path_of(f"where_popular_{city_slug(city)}.json"), lambda c=city: client.where_popular(entity_id, c))
    for city in cities:
        _save(
            path_of(f"places_{city_slug(city)}.json"),
            lambda c=city: trim_payload(client.places(entity_id, c, COMEDY_CLUB_TAG)),
        )
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("name", help="comedian name (public name only)")
    args = ap.parse_args(argv)
    out = capture(LiveClient(), args.name, fixtures_dir())
    print(f"Done: {out}")


if __name__ == "__main__":
    main()
