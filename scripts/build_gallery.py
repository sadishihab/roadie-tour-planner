#!/usr/bin/env python3
"""Build the demo gallery: <data>/gallery/<slug>.json for every <data>/fixtures/<slug>/ that has chosen.json.

<data> is ROADIE_DATA_DIR (default data/ at the repo root). Real Qloo data is private: never commit it.

Each file holds the plan, its narration and how the narration was built, so the API can serve it
without calling any model on a request. Without OPENAI_API_KEY the template narration is used.
Only the plan and narration are written; no environment value ever reaches disk.

    python scripts/build_gallery.py [--force] [--only slug ...] [--fixtures DIR] [--template]

For development, a synthetic gallery (invented comedians, template narration) goes under any data folder:
    ROADIE_DATA_DIR=/tmp/roadie-demo python scripts/build_gallery.py --fixtures tests/synthetic --template

A gallery file built with OpenAI is never replaced by a template one unless --force is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from roadie.narrator import SOURCE_OPENAI, TemplateNarrator, narrate  # noqa: E402
from roadie.pipeline import plan_tour  # noqa: E402
from roadie.paths import SLUG_RE, fixtures_dir, gallery_dir, gallery_file  # noqa: E402
from roadie.qloo_client import FixtureClient  # noqa: E402


def build_entry(slug: str, fixtures: Path | None, narrator: Any = None, now: Callable[[], datetime] | None = None) -> dict[str, Any]:
    client = FixtureClient(slug, fixtures)
    chosen = client.chosen()
    if chosen is None:
        raise ValueError(f"{slug}: no chosen.json")
    plan = plan_tour(slug, client).to_dict()
    narration = narrate(plan, narrator)
    stamp = (now or (lambda: datetime.now(timezone.utc)))()
    return {
        "slug": slug,
        "name": chosen.get("name"),
        "description": chosen.get("description"),
        "plan": plan,
        "narration": narration,
        "built_with": narration["source"],
        "built_at": stamp.replace(microsecond=0).isoformat(),
    }


def _existing_built_with(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("built_with") if isinstance(data, dict) else None


def build_gallery(
    fixtures: Path | None = None,
    out_dir: Path | None = None,
    force: bool = False,
    only: list[str] | None = None,
    narrator: Any = None,
) -> dict[str, str]:
    """Returns slug -> 'written' or 'kept' (existing OpenAI file protected from a template overwrite)."""
    fixtures = Path(fixtures) if fixtures is not None else fixtures_dir()
    out_dir = Path(out_dir) if out_dir is not None else gallery_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, str] = {}
    folders = sorted(p for p in fixtures.iterdir() if SLUG_RE.fullmatch(p.name) and (p / "chosen.json").is_file())
    for folder in folders:
        slug = folder.name
        if only and slug not in only:
            continue
        entry = build_entry(slug, fixtures, narrator)
        target = gallery_file(slug, out_dir)
        if not force and _existing_built_with(target) == SOURCE_OPENAI and entry["built_with"] != SOURCE_OPENAI:
            results[slug] = "kept"
            continue
        target.write_text(json.dumps(entry, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        results[slug] = "written"
    return results


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="let a template build replace an openai one")
    ap.add_argument("--only", nargs="*", help="build only these slugs")
    ap.add_argument("--fixtures", type=Path, help="read fixture folders from here instead of $ROADIE_DATA_DIR/fixtures (for example tests/synthetic)")
    ap.add_argument("--template", action="store_true", help="always use the template narrator, even if OPENAI_API_KEY is set")
    args = ap.parse_args(argv)
    narrator = TemplateNarrator() if args.template else None
    for slug, state in build_gallery(fixtures=args.fixtures, force=args.force, only=args.only, narrator=narrator).items():
        note = "kept existing openai file (use --force to replace)" if state == "kept" else "written"
        print(f"{slug}: {note}")


if __name__ == "__main__":
    main()
