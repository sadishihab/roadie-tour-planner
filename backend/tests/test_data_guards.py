"""Guards: real Qloo data must never be committed (the hackathon organizers do not allow it).

These look at the git index (tracked files), so they also catch a file that was force-added past
.gitignore. They need no network and no ROADIE_DATA_DIR.
"""

import json
import os
import re
import subprocess

import pytest

from synthetic import ROOT, SYNTH_DIR

# Real Qloo entity ids are uppercase UUIDs. The only ones allowed in the repo are the synthetic ids
# under tests/synthetic and the test stubs: 00000000-0000-4000-8000-<12 digits>, numbered 1 to 99999.
UPPERCASE_UUID = re.compile(r"\b[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\b")
ALLOWED_IDS = frozenset(f"00000000-0000-4000-8000-{n:012d}" for n in range(1, 100_000))
FORBIDDEN_PREFIXES = ("data/", "fixtures/", "gallery/")  # real fixtures and gallery files live only in the private data dir
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", "data"}


def tracked_files() -> list[str]:
    """Paths tracked by git; without git (an exported tarball) every file on disk outside ignored folders."""
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
        files = [p for p in out.decode("utf-8").split("\0") if p]
        if files:
            return files
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        found += [os.path.relpath(os.path.join(base, n), ROOT) for n in names]
    return found


def test_no_tracked_file_under_data_fixtures_or_gallery():
    bad = [p for p in tracked_files() if p.replace("\\", "/").startswith(FORBIDDEN_PREFIXES)]
    assert not bad, f"private Qloo data must not be tracked (keep it under ROADIE_DATA_DIR): {bad[:5]}"


def test_no_real_qloo_entity_id_in_any_tracked_file():
    offenders = []
    for rel in tracked_files():
        path = ROOT / rel
        if not path.is_file():
            continue  # deleted in the working tree but still listed
        text = path.read_bytes().decode("latin-1")
        for match in UPPERCASE_UUID.findall(text):
            if match not in ALLOWED_IDS:
                offenders.append((rel, match[:8] + "..."))  # never print a whole id
    assert not offenders, f"uppercase UUIDs (real Qloo ids?) outside the synthetic allowlist: {offenders[:5]}"


def test_the_id_scan_would_catch_a_real_looking_id():
    shaped = "-".join(["DEADBEEF", "0000", "4000", "8000", "ABCDEF012345"])  # built here so no such id is in the file
    assert UPPERCASE_UUID.search(f"id {shaped}") and shaped not in ALLOWED_IDS
    assert not UPPERCASE_UUID.search(f"id {shaped.lower()}")  # lowercase is not the Qloo form
    assert "00000000-0000-4000-8000-000000000001" in ALLOWED_IDS


def test_synthetic_set_uses_only_allowlisted_ids_and_invented_names():
    ids = set()
    for path in SYNTH_DIR.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        json.loads(text)
        ids |= set(re.findall(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", text))
    assert ids and ids <= ALLOWED_IDS
    assert all(i == i.lower() for i in ids)


def test_synthetic_fixtures_are_all_committed_and_follow_the_real_layout():
    tracked = set(tracked_files())
    from roadie.cities import CITIES
    from roadie.qloo_client import city_slug

    needed = {"chosen.json", "search.json", "openers.json", "descriptions.json", "brands.json"}
    needed |= {f"{kind}_{city_slug(c)}.json" for c in CITIES for kind in ("where_popular", "places")}
    for folder in sorted(p for p in SYNTH_DIR.iterdir() if p.is_dir()):
        names = {p.name for p in folder.iterdir()}
        assert names == needed, f"{folder.name}: {sorted(names ^ needed)}"
        assert all(f"tests/synthetic/{folder.name}/{n}" in tracked for n in names) or not (ROOT / ".git").exists()


@pytest.mark.parametrize("name", ["data", "fixtures", "gallery"])
def test_private_folders_are_git_ignored(name):
    try:
        r = subprocess.run(["git", "check-ignore", "-q", f"{name}/x.json"], cwd=ROOT, capture_output=True)
    except OSError:
        pytest.skip("git not available")
    assert r.returncode == 0, f"{name}/ must be in .gitignore"
