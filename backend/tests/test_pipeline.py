import json
import shutil
import socket
import subprocess

import pytest

from roadie.cities import CITIES, CITY_COORDS
from roadie.pipeline import (
    _resolve_comedian,
    MODERATE_GAP,
    PEER_BAND,
    QLOO,
    ROADIE,
    STRONG_GAP,
    assign_tier,
    city_interpretation,
    classify_comic,
    plan_tour,
    distance_km,
    identified_as_comedian,
    route_length_km,
    suggest_route,
)
from roadie.qloo_client import AmbiguousEntityError, FixtureClient, LiveClient, QlooError
from synthetic import HARLAN, JUNE, SOL, SYNTH_DIR, chosen_id

SLUG = JUNE
SAM = chosen_id(JUNE)  # the comedian under test (a synthetic id)


def fx(slug=SLUG):
    return FixtureClient(slug, SYNTH_DIR)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network or subprocess touched")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)


@pytest.fixture
def plan():
    return plan_tour(SLUG, fx())


def _sourced(plan):
    yield plan.comedian
    yield from plan.cities
    yield plan.city_tier_note
    yield plan.route_suggestion
    yield from plan.route_suggestion["order"]
    yield plan.comics_to_bill
    yield plan.comics_to_bill["note"]
    yield from plan.comics_to_bill["items"]
    yield from plan.sponsor_candidates["items"]
    for v in plan.comedy_venues["cities"]:
        yield from v["items"]


def test_plan_sections_and_sources(plan):
    d = plan.to_dict()
    assert set(d) == {
        "slug", "comedian", "cities", "city_tier_note", "route_suggestion", "comics_to_bill",
        "comedy_venues", "sponsor_candidates", "steps",
    }
    assert plan.comedian["entity_id"] == SAM
    for item in _sourced(plan):
        assert item["source"] in (QLOO, ROADIE)
    assert plan.comedian["source"] == QLOO
    assert plan.route_suggestion["source"] == ROADIE
    assert all(c["source"] == QLOO and c["interpretation"]["source"] == ROADIE for c in plan.cities)


def _tiers(plan):
    return {c["city"]: c["tier"] for c in plan.cities}


def _strong(plan):
    return [c["city"] for c in plan.cities if c["tier"] == "strong"]


def test_cities_keep_rank_and_affinity_with_tiers(plan):
    assert [c["rank"] for c in plan.cities] == list(range(1, 13))
    assert plan.cities[0]["city"] == "Austin, TX"
    assert all(isinstance(c["avg_affinity"], float) for c in plan.cities)
    assert all(c["tier_source"] == ROADIE for c in plan.cities)
    for c in plan.cities:
        assert "strongest neighborhoods" in c["interpretation"]["text"]


def test_tiers_on_synthetic_fixtures(plan):
    tiers = _tiers(plan)
    assert sorted(_strong(plan)) == sorted(
        ["Austin, TX", "Seattle, WA", "Chicago, IL", "New York, NY", "Philadelphia, PA", "Boston, MA"]
    )
    assert sorted(c for c, t in tiers.items() if t == "moderate") == ["Atlanta, GA", "Denver, CO"]
    assert sorted(c for c, t in tiers.items() if t == "weaker") == [
        "Dallas, TX", "Los Angeles, CA", "San Francisco, CA", "Washington, DC",
    ]
    top = plan.cities[0]["avg_affinity"]
    for c in plan.cities:
        gap = top - c["avg_affinity"]
        assert c["tier"] == ("strong" if gap <= STRONG_GAP else "moderate" if gap <= MODERATE_GAP else "weaker")


def test_assign_tier_boundaries():
    assert assign_tier(0.990, 1.0) == "strong"
    assert assign_tier(0.989, 1.0) == "moderate"
    assert assign_tier(0.980, 1.0) == "moderate"
    assert assign_tier(0.979, 1.0) == "weaker"


def test_tier_wording_and_note(plan):
    by_city = {c["city"]: c["interpretation"]["text"] for c in plan.cities}
    assert "strong audience affinity relative to the other candidate cities" in by_city["Austin, TX"].lower()
    assert "moderate audience affinity" in by_city["Atlanta, GA"].lower()
    assert "weaker audience affinity" in by_city["Dallas, TX"].lower()
    assert not any("Ranked #" in t or "Near-tie" in t for t in by_city.values())
    note = plan.city_tier_note
    assert note["source"] == ROADIE
    assert "cannot be meaningfully ordered" in note["text"]
    wording = " ".join(city_interpretation(t) for t in ("strong", "moderate", "weaker")).lower()
    assert not any(w in wording for w in ("because", "will buy", "fans in"))


def test_no_near_tie_flag_remains(plan):
    for c in plan.cities:
        assert "weak_evidence" not in c
    assert "near-tie" not in str(plan.to_dict()).lower().replace("near-ties", "")


def test_route_is_strong_tier_west_to_east(plan):
    route = plan.route_suggestion
    strong = _strong(plan)
    order = [o["city"] for o in route["order"]]
    assert sorted(order) == sorted(strong) and len(order) == 6
    assert "Dallas, TX" not in order and "Denver, CO" not in order  # moderate and weaker cities stay off the route
    assert order == ["Seattle, WA", "Austin, TX", "Chicago, IL", "Philadelphia, PA", "New York, NY", "Boston, MA"]
    assert [o["step"] for o in route["order"]] == list(range(1, 7))
    assert route["source"] == ROADIE and route["note"]["source"] == ROADIE
    assert "longitude" in route["rule"]
    for word in ("travel logistics", "dates", "routing constraints", "not a ranking", "longitude"):
        assert word in route["note"]["text"]
    lons = [CITY_COORDS[c][1] for c in order]
    assert lons == sorted(lons)
    assert all("rank" not in o for o in route["order"])


def _nearest_neighbor(cities, start):
    """The old route rule, kept here only as the baseline the sweep is compared against."""
    order, left = [start], [c for c in cities if c != start]
    while left:
        here = CITY_COORDS[order[-1]]
        nxt = min(left, key=lambda c: (distance_km(here, CITY_COORDS[c]), c))
        left.remove(nxt)
        order.append(nxt)
    return order


def test_sweep_is_shorter_than_old_nearest_neighbor_on_a_saturated_comedian():
    plan = plan_tour(SOL, fx(SOL))
    strong = _strong(plan)
    assert len(strong) == 9
    old = _nearest_neighbor(strong, min(strong, key=lambda c: (CITY_COORDS[c][1], c)))
    new = [o["city"] for o in plan.route_suggestion["order"]]
    assert sorted(old) == sorted(new)
    assert old[-2:] == ["Boston, MA", "Austin, TX"]  # the long final hop being fixed
    assert route_length_km(new) < route_length_km(old)


def test_suggest_route_unit_and_coords():
    assert set(CITY_COORDS) == set(CITIES) and len(CITIES) == 12
    assert suggest_route(["Boston, MA", "Seattle, WA", "New York, NY"]) == [
        "Seattle, WA", "New York, NY", "Boston, MA",
    ]
    with pytest.raises(KeyError):
        suggest_route(["Boston, MA", "Nowhere, ZZ"])


@pytest.fixture
def plan_without_descriptions(tmp_path):
    """A copy of the june_marlowe fixture folder where descriptions.json really is absent."""
    shutil.copytree(SYNTH_DIR / SLUG, tmp_path / SLUG)
    (tmp_path / SLUG / "descriptions.json").unlink()
    assert not (tmp_path / SLUG / "descriptions.json").exists()
    return plan_tour(SLUG, FixtureClient(SLUG, tmp_path))


def test_comics_to_bill_with_captured_descriptions_identifies_comedians(plan):
    items = plan.comics_to_bill["items"]
    assert len(items) == 10
    by_name = {c["name"]: c["identified_as_comedian"] for c in items}
    assert sum(v is True for v in by_name.values()) == 6  # mostly identified
    assert by_name["Pilar Mondragon-Ash"] is False  # "Fictional television chef"
    assert by_name["Saffron Teague"] is False  # captured, but the description is null
    assert by_name["Lyle Pennywhistle"] is None  # absent from a captured file: unknown, not false
    flags = [c["identified_as_comedian"] is not True for c in items]
    assert flags == sorted(flags)  # identified comedians first
    assert "Descriptions have not been captured" not in plan.comics_to_bill["note"]["text"]


def test_comics_to_bill_without_descriptions_marks_unknown(plan_without_descriptions):
    section = plan_without_descriptions.comics_to_bill
    items = section["items"]
    assert len(items) == 10
    assert all(c["entity_id"].upper() != SAM for c in items)
    assert all(c["relation"] in ("peer", "bigger act", "smaller act") for c in items)
    rel = {c["name"]: c["relation"] for c in items}
    assert rel["Wren Alderfoot"] == "peer"  # 0.57 vs 0.55
    assert rel["Marnie Fenwick-Doyle"] == "bigger act"  # 0.80
    assert rel["Dax Holloway-Pike"] == "smaller act"  # 0.20
    assert all(c["interpretation"]["source"] == ROADIE for c in items)
    # no descriptions.json: unknown (None), never false, original order kept
    assert all(c["identified_as_comedian"] is None and c["identified_source"] == ROADIE for c in items)
    assert [c["affinity"] for c in items] == sorted((c["affinity"] for c in items), reverse=True)
    assert section["source"] == ROADIE and section["note"]["source"] == ROADIE
    text = section["note"]["text"]
    for phrase in ("overlapping audiences", "not necessarily comedians", "simple text match", "can miss real comics"):
        assert phrase in text


class _Described(FixtureClient):
    def __init__(self, slug, descriptions):
        super().__init__(slug, SYNTH_DIR)
        self._descriptions = descriptions

    def descriptions(self):
        return self._descriptions


def test_comics_to_bill_identifies_and_orders_comedians_first():
    base = fx().similar(SAM)
    ids = [o["entity_id"] for o in base]
    descs = {i: "American politician" for i in ids}
    descs[ids[3]] = "American Stand-Up Comedian"
    descs[ids[6]] = "Comic and podcaster"
    descs[ids[8]] = None  # captured, but Qloo had no description: false, not unknown
    del descs[ids[9]]  # absent from a captured file: unknown
    plan = plan_tour(SLUG, _Described(SLUG, descs))
    items = plan.comics_to_bill["items"]
    assert [c["entity_id"] for c in items[:2]] == [ids[3], ids[6]]
    assert [c["identified_as_comedian"] for c in items[:2]] == [True, True]
    rest = items[2:]
    assert [c["entity_id"] for c in rest] == [i for i in ids if i not in (ids[3], ids[6])]  # order kept
    by_id = {c["entity_id"]: c["identified_as_comedian"] for c in items}
    assert by_id[ids[0]] is False and by_id[ids[8]] is False and by_id[ids[9]] is None
    assert items[0]["description"] == "American Stand-Up Comedian"
    assert "descriptions have not been captured" not in plan.comics_to_bill["note"]["text"].lower()


def test_identified_as_comedian_text_match():
    assert identified_as_comedian("Stand-up COMEDIAN and actor", True) is True
    assert identified_as_comedian("American comics writer", True) is True
    assert identified_as_comedian("American politician", True) is False
    assert identified_as_comedian("Comedy writer", True) is False  # a simple match misses this
    assert identified_as_comedian(None, True) is False
    assert identified_as_comedian("Comedian", False) is None


def test_comics_drops_the_comedian_itself(tmp_path):
    class Self(FixtureClient):
        def __init__(self, slug):
            super().__init__(slug, SYNTH_DIR)

        def similar(self, entity_id):
            return [{"name": "June Marlowe", "entity_id": SAM.lower(), "popularity": 0.84, "query": {"affinity": 1}}]

    assert plan_tour(SLUG, Self(SLUG)).comics_to_bill["items"] == []


def test_comedy_venues(plan):
    venues = plan.comedy_venues
    route_cities = [o["city"] for o in plan.route_suggestion["order"]]
    assert [v["city"] for v in venues["cities"]] == route_cities
    for v in venues["cities"]:
        assert v["label"] == f"comedy venues this audience favors in {v['city']}"
        assert len(v["items"]) == len({i["name"] for i in v["items"]})
    for word in ("capacity", "booking policy", "stand-up, improv and sketch"):
        assert word in venues["limits"]["text"]
    assert venues["limits"]["source"] == ROADIE


def test_limited_data_note_when_fewer_than_five_venues(plan):
    by_city = {v["city"]: v for v in plan.comedy_venues["cities"]}
    assert len(by_city["Philadelphia, PA"]["items"]) == 4
    assert by_city["Philadelphia, PA"]["limited_data_note"]["source"] == ROADIE
    assert "Limited data" in by_city["Philadelphia, PA"]["limited_data_note"]["text"]
    assert len(by_city["Seattle, WA"]["items"]) == 3 and "limited_data_note" in by_city["Seattle, WA"]
    for v in by_city.values():
        assert ("limited_data_note" in v) == (len(v["items"]) < 5)
    assert "limited_data_note" not in by_city["New York, NY"]
    assert "limited_data_note" not in by_city["Boston, MA"]  # exactly 5 is enough


def test_city_with_no_venues_is_flagged_not_hidden(plan):
    austin = next(v for v in plan.comedy_venues["cities"] if v["city"] == "Austin, TX")
    assert austin["items"] == [] and austin["note"] == "not captured"
    assert "only 0 comedy venues" in austin["limited_data_note"]["text"]


def test_missing_places_file_is_a_flagged_empty_list(tmp_path):
    shutil.copytree(SYNTH_DIR / SLUG, tmp_path / SLUG)
    (tmp_path / SLUG / "places_chicago.json").unlink()
    plan = plan_tour(SLUG, FixtureClient(SLUG, tmp_path))
    chicago = next(v for v in plan.comedy_venues["cities"] if v["city"] == "Chicago, IL")
    assert chicago["items"] == [] and "limited_data_note" in chicago


def test_sponsor_candidates(plan):
    s = plan.sponsor_candidates
    assert s["label"] == "brands with audience overlap worth approaching"
    assert len(s["items"]) == 10 and s["items"][0]["name"] == "Northwind Provisions"
    assert "audience overlap, not sponsorship intent" in s["limits"]["text"]


def test_cut_sections_absent(plan):
    d = plan.to_dict()
    for gone in ("vibe", "shared_audience", "trends", "openers", "venues"):
        assert gone not in d


def test_steps_are_ordered_and_streamed():
    seen = []
    plan = plan_tour(SLUG, fx(), on_step=seen.append)
    assert [s.step for s in plan.steps] == [
        "resolve_comedian", "rank_cities", "route_suggestion", "find_comics_to_bill",
        "comedy_venues", "sponsor_candidates",
    ]
    assert seen == plan.steps
    assert all(s.queried and s.summary and s.source in (QLOO, ROADIE) for s in plan.steps)


def test_classify_comic():
    assert classify_comic(0.95, 0.8) == "bigger act"
    assert classify_comic(0.6, 0.8) == "smaller act"
    assert classify_comic(0.8 + PEER_BAND, 0.8) == "peer"


def test_rejects_live_client():
    with pytest.raises(TypeError):
        plan_tour(SLUG, LiveClient())


def test_ambiguous_name_raises(tmp_path):
    # two same-name results and no chosen.json: still a human's call
    folder = tmp_path / SLUG
    folder.mkdir()
    (folder / "search.json").write_text(
        json.dumps([{"name": "June Marlowe", "entity_id": "1"}, {"name": "June Marlowe", "entity_id": "2"}])
    )
    assert FixtureClient(SLUG, tmp_path).chosen() is None
    with pytest.raises(AmbiguousEntityError):
        plan_tour(SLUG, FixtureClient(SLUG, tmp_path))


def test_chosen_json_resolves_same_name_results(tmp_path):
    folder = tmp_path / SLUG
    folder.mkdir()
    results = [
        {"name": "June Marlowe", "entity_id": "1", "popularity": 0.1},
        {"name": "June Marlowe", "entity_id": "2", "popularity": 0.2},
    ]
    (folder / "search.json").write_text(json.dumps(results))
    (folder / "chosen.json").write_text(json.dumps({"entity_id": "2", "name": "June Marlowe", "description": None}))
    assert _resolve_comedian(SLUG, FixtureClient(SLUG, tmp_path)) == {
        "name": "June Marlowe", "entity_id": "2", "popularity": 0.2,
    }
    (folder / "chosen.json").write_text(json.dumps({"entity_id": "9", "name": "June Marlowe", "description": None}))
    with pytest.raises(QlooError):
        _resolve_comedian(SLUG, FixtureClient(SLUG, tmp_path))


ALL_SLUGS = [JUNE, HARLAN, SOL]


@pytest.mark.parametrize("slug", ALL_SLUGS)
def test_plan_tour_runs_for_every_synthetic_comedian(slug):
    client = fx(slug)
    chosen = client.chosen()
    assert set(chosen) == {"entity_id", "name", "description"}
    plan = plan_tour(slug, client)
    assert plan.comedian["entity_id"] == chosen["entity_id"]
    assert plan.cities and plan.steps[0].step == "resolve_comedian"


def test_ambiguous_search_is_resolved_by_chosen_json():
    results = fx(SOL).search_person("Sol Ambrose")
    same = [r for r in results if r["name"] == "Sol Ambrose"]
    assert len(same) == 2 and same[0]["entity_id"] != same[1]["entity_id"]
    plan = plan_tour(SOL, fx(SOL))
    assert plan.comedian["entity_id"] == chosen_id(SOL)
    assert plan.comedian["popularity"] == 0.7  # the chosen one, not the 0.4 namesake


def test_ambiguous_search_without_chosen_json_raises(tmp_path):
    shutil.copytree(SYNTH_DIR / SOL, tmp_path / SOL)
    (tmp_path / SOL / "chosen.json").unlink()
    with pytest.raises(AmbiguousEntityError):
        plan_tour(SOL, FixtureClient(SOL, tmp_path))


def test_comedian_with_no_identified_comics():
    plan = plan_tour(HARLAN, fx(HARLAN))
    items = plan.comics_to_bill["items"]
    assert len(items) == 10 and all(c["identified_as_comedian"] is False for c in items)
    assert "Descriptions have not been captured" not in plan.comics_to_bill["note"]["text"]
    assert [o["city"] for o in plan.route_suggestion["order"]] == ["Los Angeles, CA", "New York, NY"]
    assert all("limited_data_note" not in v for v in plan.comedy_venues["cities"])
