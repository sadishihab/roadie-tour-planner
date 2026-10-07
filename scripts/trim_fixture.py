#!/usr/bin/env python3
"""Trim saved place/brand insights fixtures in place (offline, no Qloo calls).

Files are named as <slug>/<file>.json inside the private fixtures folder (ROADIE_DATA_DIR/fixtures,
or --root); nothing outside it can be named.

    python scripts/trim_fixture.py some_comedian/places_chicago.json some_comedian/brands.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from roadie.paths import fixture_file, fixtures_dir  # noqa: E402
from roadie.trim import trim_payload  # noqa: E402


def trim_file(path: Path) -> tuple[int, int]:
    before = path.stat().st_size
    data = trim_payload(json.loads(path.read_text(encoding="utf-8")))
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return before, path.stat().st_size


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", help="<slug>/<file>.json under the fixtures folder")
    ap.add_argument("--root", type=Path, default=None, help="fixtures folder (default: ROADIE_DATA_DIR/fixtures)")
    args = ap.parse_args(argv)
    root = args.root if args.root is not None else fixtures_dir()
    for name in args.files:
        slug, _, filename = name.partition("/")
        path = fixture_file(slug, filename, root)
        before, after = trim_file(path)
        print(f"{name}: {before} -> {after} bytes")


if __name__ == "__main__":
    main()
