import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from roadie.paths import fixture_folder
from roadie.qloo_client import AmbiguousEntityError, FixtureClient, LiveClient, QlooError, city_slug
from synthetic import JUNE, SYNTH_DIR, TAG, chosen_id

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import capture_comedian  # noqa: E402

SAM = chosen_id(JUNE)


def completed(stdout="{}", returncode=0, stderr=""):
    return subprocess.CompletedProcess(["qloo"], returncode, stdout, stderr)


@pytest.fixture
def run(monkeypatch):
    calls = []
    results = []

    def fake(cmd, **kwargs):
        calls.append((cmd, kwargs))
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(subprocess, "run", fake)
    fake.calls, fake.results = calls, results
    return fake


def live():
    return LiveClient(sleep=lambda s: None)


def test_exec_shape(run):
    run.results.append(completed("{}"))
    live().where_popular("ID", "Austin, TX")
    cmd, kwargs = run.calls[0]
    assert cmd[:3] == ["qloo", "exec", "where_popular"]
    assert json.loads(cmd[cmd.index("--input") + 1]) == {"entity": "ID", "within": "Austin, TX"}
    assert kwargs["timeout"] == 30.0 and kwargs["capture_output"]


def test_person_cli_commands(run):
    run.results.extend([completed("[]"), completed('{"results": []}'), completed("[]"), completed("[]")])
    c = live()
    assert c.search_person("June Marlowe") == []
    assert c.similar("ID") == []
    c.places("ID", "Chicago, IL", TAG)
    c.brands("ID")
    cmds = [cmd for cmd, _ in run.calls]
    assert cmds[0] == ["qloo", "api", "search", "--query", "June Marlowe", "--type", "person", "--take", "3", "--json"]
    assert cmds[1] == [
        "qloo", "api", "insights", "--type", "person", "--signal-entities", "ID", "--take", "10", "--json",
    ]
    assert cmds[2] == [
        "qloo", "api", "insights", "--type", "place", "--signal-entities", "ID",
        "--filter-location", "Chicago, IL", "--filter-tags", TAG, "--take", "10", "--json",
    ]
    assert cmds[3] == [
        "qloo", "api", "insights", "--type", "brand", "--signal-entities", "ID", "--take", "10", "--json",
    ]
    assert all("--signal-query" not in cmd for cmd in cmds)  # by ID, never by name


def test_retries_then_succeeds(run):
    run.results.extend([subprocess.TimeoutExpired("qloo", 30), completed(returncode=1, stderr="boom"), completed('{"a": 1}')])
    assert live().where_popular("ID", "Austin, TX") == {"a": 1}
    assert len(run.calls) == 3


def test_retries_are_bounded(run):
    run.results.extend([completed(returncode=1, stderr="boom")] * 5)
    with pytest.raises(QlooError):
        live().where_popular("ID", "Austin, TX")
    assert len(run.calls) == 3  # first try + 2 retries


def test_retries_cannot_be_raised(run):
    run.results.extend([completed("not json")] * 10)
    with pytest.raises(QlooError):
        LiveClient(retries=50, sleep=lambda s: None).where_popular("ID", "Austin, TX")
    assert len(run.calls) == 3


def test_needs_input_is_not_retried(run):
    run.results.append(completed('{"status": "needs_input"}'))
    with pytest.raises(AmbiguousEntityError):
        live().where_popular("Name", "Austin, TX")
    assert len(run.calls) == 1


def test_needs_input_on_search_raises(run):
    run.results.append(completed('{"status": "needs_input"}'))
    with pytest.raises(AmbiguousEntityError):
        live().search_person("Sam")


def test_no_key_handling(run):
    run.results.append(completed("{}"))
    live().where_popular("ID", "Austin, TX")
    cmd, kwargs = run.calls[0]
    assert "env" not in kwargs  # environment is inherited untouched
    assert not any("key" in part.lower() for part in cmd)


def test_fixture_client_synthetic():
    c = FixtureClient(JUNE, SYNTH_DIR)
    assert c.search_person("June Marlowe")[0]["entity_id"] == SAM
    assert c.where_popular(SAM, "Austin, TX")["interpretation"]["within"] == "Austin, TX"
    assert c.similar(SAM)[0]["name"] == "Wren Alderfoot"
    assert c.places(SAM, "Chicago, IL", TAG)[0]["name"].startswith("The ")
    assert c.brands(SAM)[0]["name"] == "Northwind Provisions"
    assert c.chosen()["entity_id"] == SAM


def test_person_description_runs_entity_lookup_and_returns_only_the_string(run):
    entity = {
        "name": "X", "entity_id": "ID",
        "properties": {"short_description": "American politician"},
        "tags": [{"name": "American Lgbt Artists"}],
    }
    run.results.append(completed(json.dumps(entity)))
    assert live().person_description("ID") == "American politician"
    assert run.calls[0][0] == ["qloo", "api", "entity", "--id", "ID", "--json"]
    for wrapped in ({"results": [entity]}, [entity]):
        run.results.append(completed(json.dumps(wrapped)))
        assert live().person_description("ID") == "American politician"


@pytest.mark.parametrize("payload", [{"properties": {}}, {"properties": {"short_description": "  "}}, {}, {"results": []}, []])
def test_person_description_none_when_missing(run, payload):
    run.results.append(completed(json.dumps(payload)))
    assert live().person_description("ID") is None


def test_fixture_client_descriptions_file():
    descriptions = FixtureClient(JUNE, SYNTH_DIR).descriptions()
    assert descriptions and all(isinstance(k, str) for k in descriptions)
    known = next(k for k, v in descriptions.items() if v)
    assert FixtureClient(JUNE, SYNTH_DIR).person_description(known) == descriptions[known]
    assert FixtureClient(JUNE, SYNTH_DIR).person_description("no-such-id") is None


def test_fixture_client_descriptions_file_absent(tmp_path):
    shutil.copytree(SYNTH_DIR / JUNE, tmp_path / JUNE)
    (tmp_path / JUNE / "descriptions.json").unlink()
    c = FixtureClient(JUNE, tmp_path)
    assert c.descriptions() == {}  # not captured: empty map
    assert c.person_description(SAM) is None
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "descriptions.json").write_text('{"A": "Comedian", "B": null}')
    c = FixtureClient("x", tmp_path)
    assert c.descriptions() == {"A": "Comedian", "B": None}
    assert c.person_description("A") == "Comedian"


def test_fixture_client_missing():
    with pytest.raises(FileNotFoundError):
        FixtureClient(JUNE, SYNTH_DIR).where_popular(SAM, "Nowhere, ZZ")
    with pytest.raises(FileNotFoundError):
        FixtureClient(JUNE, SYNTH_DIR).places(SAM, "Nowhere, ZZ", TAG)


def test_city_slug():
    assert city_slug("Los Angeles, CA") == "los_angeles"


def test_capture_cities_constant_is_twelve():
    from roadie.cities import CITIES

    assert capture_comedian.CITIES is CITIES and len(CITIES) == 12


RAW_ENTITY = {
    "name": "X", "entity_id": "E", "type": "urn:entity", "subtype": "urn:entity:place", "popularity": 0.5,
    "query": {"affinity": 0.9, "distance": 0}, "tags": [{"id": "t", "name": "Bar", "type": "u"}],
    "properties": {"phone": "1", "short_description": "d", "hours": {}},
}


def _where_popular(city, affinity):
    cells = [{"query": {"affinity": affinity, "popularity": 0.5}}] * 2
    return {"operation": "where_popular", "status": "ok", "interpretation": {"within": city}, "results": cells}


class Stub(FixtureClient):
    def __init__(self):
        self.calls = []

    def search_person(self, name):
        self.calls.append("search")
        return [{"name": "Test Comic", "entity_id": "T1"}]

    def similar(self, entity_id):
        self.calls.append("similar")
        return [{**RAW_ENTITY, "subtype": "urn:entity:person", "tags": [{"name": "Some Category"}]}]

    def where_popular(self, entity_id, city):
        self.calls.append(f"where:{city}")
        return _where_popular(city, 0.9)

    def places(self, entity_id, city, category_tag):
        assert category_tag == TAG
        self.calls.append(f"places:{city}")
        return [RAW_ENTITY]

    def brands(self, entity_id):
        self.calls.append("brands")
        return [RAW_ENTITY]

    def person_description(self, entity_id):
        self.calls.append(f"describe:{entity_id}")
        return "American politician"


def test_capture_saves_everything_trimmed_and_skips_existing(tmp_path, monkeypatch):
    answers = []
    monkeypatch.setattr("builtins.input", lambda _: answers.append(1) or "1")
    cities = ["Austin, TX", "Denver, CO"]
    stub = Stub()
    out = capture_comedian.capture(stub, "Test Comic", tmp_path, cities)
    assert answers == [1]  # a human confirmed the match, even with a single result
    assert stub.calls == [
        "search", "similar", "describe:E", "brands", "where:Austin, TX", "where:Denver, CO",
        "places:Austin, TX", "places:Denver, CO",
    ]
    assert sorted(p.name for p in out.iterdir()) == sorted(
        ["search.json", "chosen.json", "openers.json", "descriptions.json", "brands.json", "where_popular_austin.json",
         "where_popular_denver.json", "places_austin.json", "places_denver.json"]
    )
    assert json.loads((out / "descriptions.json").read_text()) == {"E": "American politician"}
    assert json.loads((out / "chosen.json").read_text()) == {"entity_id": "T1", "name": "Test Comic", "description": None}
    for name in ("places_denver.json", "brands.json"):
        assert json.loads((out / name).read_text()) == [{
            "name": "X", "entity_id": "E", "type": "urn:entity", "subtype": "urn:entity:place",
            "popularity": 0.5, "query": {"affinity": 0.9}, "properties": {"short_description": "d"},
            "tags": [{"name": "Bar"}],
        }]
    # openers are people: saved without tags or subtype
    assert json.loads((out / "openers.json").read_text()) == [{
        "name": "X", "entity_id": "E", "type": "urn:entity", "popularity": 0.5, "query": {"affinity": 0.9},
        "properties": {"short_description": "d"},
    }]
    stub2 = Stub()
    capture_comedian.capture(stub2, "Test Comic", tmp_path, cities)
    assert stub2.calls == []  # every file exists: no Qloo calls, search.json reused


def test_capture_aborts_when_human_declines(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    with pytest.raises(SystemExit):
        capture_comedian.capture(Stub(), "Test Comic", tmp_path, ["Austin, TX"])
    assert not (tmp_path / "test_comic" / "search.json").exists()


@pytest.mark.parametrize("slug", ["", "..", "../x", "a/b", "A", "x y", "x" * 41, "x\n", "/etc"])
def test_fixture_client_rejects_bad_slugs(slug):
    with pytest.raises(ValueError):
        FixtureClient(slug, SYNTH_DIR)


def test_fixture_client_default_root_follows_roadie_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ROADIE_DATA_DIR", str(tmp_path))
    assert fixture_folder("some_comedian") == tmp_path / "fixtures" / "some_comedian"
    assert FixtureClient("some_comedian").dir == tmp_path / "fixtures" / "some_comedian"
