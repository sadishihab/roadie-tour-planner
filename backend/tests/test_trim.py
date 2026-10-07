import json
from pathlib import Path

import pytest

from roadie.trim import trim_entity, trim_payload
from synthetic import JUNE, ROOT, SYNTH_DIR

SYNTH_FOLDERS = sorted(p for p in SYNTH_DIR.iterdir() if p.is_dir())
RAW = {
    "name": "Sleeping Village", "entity_id": "ID1", "type": "urn:entity", "subtype": "urn:entity:place",
    "popularity": 0.99, "query": {"affinity": 0.97, "distance": 0, "measurements": {"audience_growth": 0}},
    "tags": [{"id": "a", "name": "Spirits", "type": "t", "weight": None}, {"id": "b", "name": "Bar"}],
    "properties": {"short_description": "A bar.", "phone": "+1", "hours": {"monday": []}, "description": "long"},
    "external": {"google_place": [{"id": "x"}]}, "location": {"lat": 1}, "enabled": True,
}


def test_trim_entity_keeps_only_listed_fields_at_original_paths():
    assert trim_entity(RAW) == {
        "name": "Sleeping Village", "entity_id": "ID1", "type": "urn:entity", "subtype": "urn:entity:place",
        "popularity": 0.99, "query": {"affinity": 0.97}, "properties": {"short_description": "A bar."},
        "tags": [{"name": "Spirits"}, {"name": "Bar"}],
    }


def test_trim_entity_tolerates_missing_fields():
    assert trim_entity({"name": "A", "entity_id": "1"}) == {"name": "A", "entity_id": "1"}


def test_trim_payload_keeps_top_level_shape():
    assert trim_payload([RAW]) == [trim_entity(RAW)]
    wrapped = trim_payload({"status": "ok", "results": [RAW]})
    assert wrapped == {"status": "ok", "results": [trim_entity(RAW)]}
    with pytest.raises(ValueError):
        trim_payload({"nothing": 1})


def test_trim_is_idempotent():
    once = trim_payload([RAW])
    assert trim_payload(once) == once


@pytest.mark.parametrize("name", ["places_chicago.json", "brands.json"])
def test_synthetic_fixtures_are_trimmed(name):
    path = SYNTH_DIR / JUNE / name
    data = json.loads(path.read_text())
    assert path.stat().st_size < 100_000
    assert len(data) in (7, 10) and trim_payload(data) == data
    assert all(e["query"]["affinity"] and e["tags"] for e in data)  # venue and brand tags are kept


RAW_PERSON = {
    "name": "A Person", "entity_id": "P1", "type": "urn:entity", "subtype": "urn:entity:person",
    "popularity": 0.7, "query": {"affinity": 0.95, "distance": 0},
    "tags": [{"id": "a", "name": "American Lgbt Artists"}, {"name": "Actor"}],
    "properties": {"short_description": "American politician", "akas": [{"value": "x"}]},
}


def test_trim_person_never_keeps_tags_or_other_fields():
    assert trim_entity(RAW_PERSON) == {
        "name": "A Person", "entity_id": "P1", "type": "urn:entity", "popularity": 0.7,
        "query": {"affinity": 0.95}, "properties": {"short_description": "American politician"},
    }
    search_hit = {
        "name": "A Person", "entity_id": "P1", "types": ["urn:entity:person"], "popularity": 0.7,
        "tags": [{"name": "Actor"}], "disambiguation": "d", "properties": {"short_description": "Comedian"},
    }
    assert trim_entity(search_hit) == {
        "name": "A Person", "entity_id": "P1", "popularity": 0.7, "properties": {"short_description": "Comedian"},
    }
    assert trim_payload([RAW_PERSON, RAW])[1]["tags"]  # non-person entities still keep tag names


PERSON_FILES = sorted(SYNTH_DIR.glob("*/openers.json")) + sorted(SYNTH_DIR.glob("*/search.json"))
SENSITIVE_WORDS = ("lgbt", "gay", "lesbian", "jewish", "muslim", "catholic", "christian")


def test_person_fixtures_are_found():
    # discovered by glob, so adding a comedian needs no edit here
    assert PERSON_FILES
    for folder in SYNTH_FOLDERS:
        if (folder / "openers.json").is_file():
            assert folder / "openers.json" in PERSON_FILES
        if (folder / "search.json").is_file():
            assert folder / "search.json" in PERSON_FILES


@pytest.mark.parametrize("path", PERSON_FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_person_fixtures_have_no_tags_field(path):
    data = json.loads(path.read_text())
    assert data and all("tags" not in e for e in data)
    assert trim_payload(data) == data


@pytest.mark.parametrize("path", PERSON_FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_person_fixtures_have_no_sensitive_words(path):
    text = path.read_text().lower()
    found = [w for w in SENSITIVE_WORDS if w in text]
    assert not found, f"{path} contains {found}"


def test_trim_fixture_script_trims_in_place(tmp_path, monkeypatch):
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import trim_fixture

    (tmp_path / "some_comedian").mkdir()
    f = tmp_path / "some_comedian" / "places_x.json"
    f.write_text(json.dumps([RAW]))
    trim_fixture.main(["some_comedian/places_x.json", "--root", str(tmp_path)])
    assert json.loads(f.read_text()) == [trim_entity(RAW)]


@pytest.mark.parametrize("name", ["../x/places.json", "some_comedian/../../etc.json", "/etc/passwd", "some_comedian/a.txt", "Some/places.json", "x/y/z.json"])
def test_trim_fixture_script_refuses_paths_outside_the_fixtures_folder(tmp_path, name):
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import trim_fixture

    with pytest.raises(ValueError):
        trim_fixture.main([name, "--root", str(tmp_path)])
