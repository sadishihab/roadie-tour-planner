"""The one place that turns ROADIE_DATA_DIR and a slug into a path.

Real Qloo responses (fixtures) and the gallery built from them are private data: they live under
ROADIE_DATA_DIR (default ``data/`` at the repo root, git-ignored), in ``fixtures/<slug>/`` and
``gallery/<slug>.json``. They must never be committed. Everything that reads or writes those
files goes through these helpers, which also keep the slug rules and block path traversal.
Tests use the synthetic set under ``tests/synthetic`` and pass their own roots.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

DATA_DIR_ENV = "ROADIE_DATA_DIR"
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
SLUG_RE = re.compile(r"^[a-z0-9_]{1,40}$")
FIXTURE_FILE_RE = re.compile(r"^[a-z0-9_]{1,60}\.json$")


def data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """ROADIE_DATA_DIR, or ``data/`` at the repo root. The folder need not exist."""
    env = os.environ if environ is None else environ
    value = (env.get(DATA_DIR_ENV) or "").strip()
    return Path(value).expanduser() if value else DEFAULT_DATA_DIR


def fixtures_dir(data: Path | None = None) -> Path:
    return (data if data is not None else data_dir()) / "fixtures"


def gallery_dir(data: Path | None = None) -> Path:
    return (data if data is not None else data_dir()) / "gallery"


def check_slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ValueError("invalid slug")
    return slug


def _inside(root: Path, child: Path) -> Path:
    if child.resolve().parent != root.resolve():
        raise ValueError("path escapes its folder")
    return child


def fixture_folder(slug: str, root: Path | None = None) -> Path:
    """``<root>/<slug>`` (root defaults to the private fixtures folder). Raises ValueError on a bad slug."""
    base = Path(root) if root is not None else fixtures_dir()
    return _inside(base, base / check_slug(slug))


def fixture_file(slug: str, filename: str, root: Path | None = None) -> Path:
    """``<root>/<slug>/<filename>`` for a plain ``name.json`` filename."""
    if not isinstance(filename, str) or not FIXTURE_FILE_RE.fullmatch(filename):
        raise ValueError("invalid fixture file name")
    folder = fixture_folder(slug, root)
    return _inside(folder, folder / filename)


def gallery_file(slug: str, root: Path | None = None) -> Path:
    """``<gallery>/<slug>.json`` (the gallery folder defaults to the private one)."""
    base = Path(root) if root is not None else gallery_dir()
    return _inside(base, base / f"{check_slug(slug)}.json")
