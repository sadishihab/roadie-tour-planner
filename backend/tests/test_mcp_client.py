"""The persistent Qloo harness and McpClient, against a scripted fake child. No network, no real subprocess."""

import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from fake_mcp import FakeChild, FixtureServer, Spawner, entities_doc, tool_error, wait_for, wrap
from roadie import mcp_client as mc
from roadie.api import create_app
from roadie.live import REDUCED_CITIES, LiveService, RatePacer, default_client_factory
from roadie.mcp_client import BAD_RESPONSE, TIMEOUT, TOOL_ERROR, UNAVAILABLE, HarnessError, McpClient, PersistentHarness
from roadie.qloo_client import FixtureClient, LiveClient, QlooClient
from roadie.ranking import _load_city
from roadie.settings import Settings
from synthetic import JUNE, SYNTH_DIR

CANARY = "canary-secret-7f3a9"
IDS = [f"00000000-0000-4000-8000-{9000 + i:012d}" for i in range(10)]
EID = "00000000-0000-4000-8000-000000000001"
FX = FixtureClient(JUNE, SYNTH_DIR)
CHICAGO = "Chicago, IL"
TAG = "urn:tag:category:place:comedy_club"


@pytest.fixture
def harnesses():
    made = []

    def build(*children, **kw):
        kw.setdefault("sleep", lambda s: None)
        spawner = Spawner(*children)
        h = PersistentHarness(spawn=spawner, **kw)
        h.spawner = spawner
        made.append(h)
        return h

    yield build
    for h in made:
        h.stop()


def ready(h):
    h.start()
    assert wait_for(lambda: h.status in ("ready", "unavailable"))
    return h


# ------------------------------------------------------------------------ handshake


def test_handshake_sends_initialize_then_initialized_then_tools_list_in_order(harnesses):
    child = FakeChild()
    h = ready(harnesses(child))
    assert h.status == "ready" and h.ready
    methods = [m["method"] for m in child.received]
    assert methods == ["initialize", "notifications/initialized", "tools/list"]
    init = child.received[0]
    assert init["jsonrpc"] == "2.0" and init["id"] == 1
    assert init["params"] == {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "roadie", "version": "0"}}
    assert "id" not in child.received[1]  # a notification has no id
    assert set(h.tool_names) == set(mc.REQUIRED_TOOLS) | {"qloo_find_tags"}


def test_not_ready_until_initialize_answers(harnesses):
    gate = threading.Event()

    class Slow(FixtureServer):
        def handle(self, msg, child):
            if msg.get("method") == "initialize":
                gate.wait(5)
            return super().handle(msg, child)

    h = harnesses(FakeChild(Slow()))
    h.start()
    time.sleep(0.05)
    assert h.status == "starting" and not h.ready
    gate.set()
    assert wait_for(lambda: h.ready)


def test_a_missing_required_tool_fails_the_start(harnesses):
    only_find = lambda: FakeChild(FixtureServer(tools=["qloo_find_tags"]))
    h = ready(harnesses(only_find, only_find, only_find, only_find))
    assert h.status == "unavailable"


# -------------------------------------------------------------- matching and errors


def test_responses_are_matched_by_id_even_when_they_arrive_out_of_order(harnesses):
    class Reverse(FixtureServer):
        """Holds the first three tool calls and answers them last-in-first-out."""

        def __init__(self):
            super().__init__()
            self.held = []

        def handle(self, msg, child):
            if msg.get("method") != "tools/call":
                return super().handle(msg, child)
            with self.lock:
                self.held.append((msg["id"], msg["params"]))
                full = len(self.held) == 3
            if full:
                for rid, p in reversed(self.held):
                    child.reply(rid, self.tool_result(p["name"], p["arguments"]))
            return None

    h = ready(harnesses(FakeChild(Reverse()), max_in_flight=3))
    out = {}

    def call(city):
        out[city] = h.call_tool("qloo_where_popular", {"entity": EID, "within": city})

    cities = ["Chicago, IL", "Austin, TX", "Seattle, WA"]
    threads = [threading.Thread(target=call, args=(c,)) for c in cities]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    for city in cities:
        assert out[city] == FX.where_popular(EID, city)  # each caller got its own document


def test_a_tool_error_becomes_a_fixed_code_never_the_raw_message(harnesses, caplog):
    class Bad(FixtureServer):
        def tool_result(self, name, args):
            return tool_error(f"Invalid recommend input: /signals: must be array {CANARY} --input qloo mcp")

    caplog.set_level(logging.DEBUG)
    h = ready(harnesses(FakeChild(Bad())))
    with pytest.raises(HarnessError) as e:
        h.call_tool("qloo_recommend", {"signals": EID})
    assert e.value.code == TOOL_ERROR and str(e.value) == TOOL_ERROR and CANARY not in repr(e.value)
    assert h.ready  # a tool error is not a broken process
    assert CANARY not in "\n".join(r.getMessage() for r in caplog.records)


def test_a_non_json_or_missing_document_is_a_bad_response(harnesses):
    class Odd(FixtureServer):
        def tool_result(self, name, args):
            return {"content": [{"type": "text", "text": f"not json {CANARY}"}], "isError": False}

    h = ready(harnesses(FakeChild(Odd())))
    with pytest.raises(HarnessError) as e:
        h.call_tool("qloo_where_popular", {})
    assert e.value.code == BAD_RESPONSE and CANARY not in str(e.value)


def test_a_json_rpc_error_response_is_a_fixed_code(harnesses):
    class Rpc(FixtureServer):
        def handle(self, msg, child):
            if msg.get("method") == "tools/call":
                child._out.put(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -1, "message": CANARY}}) + "\n")
                return None
            return super().handle(msg, child)

    h = ready(harnesses(FakeChild(Rpc())))
    with pytest.raises(HarnessError) as e:
        h.call_tool("qloo_where_popular", {})
    assert e.value.code == TOOL_ERROR and CANARY not in str(e.value)


def test_junk_lines_and_server_notifications_are_ignored(harnesses):
    class Noisy(FixtureServer):
        def handle(self, msg, child):
            child._out.put("this is not json\n")
            child._out.put(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {"x": 1}}) + "\n")
            return super().handle(msg, child)

    h = ready(harnesses(FakeChild(Noisy())))
    assert h.call_tool("qloo_where_popular", {"entity": EID, "within": CHICAGO}) == FX.where_popular(EID, CHICAGO)


# ------------------------------------------------------------------------ timeouts


def test_a_call_that_times_out_fails_cleanly_and_does_not_block_later_calls(harnesses):
    class SilentOnce(FixtureServer):
        def __init__(self):
            super().__init__()
            self.first = None

        def handle(self, msg, child):
            if msg.get("method") == "tools/call" and self.first is None:
                self.first = msg["id"]
                return None  # never answered in time
            return super().handle(msg, child)

    server = SilentOnce()
    child = FakeChild(server)
    h = ready(harnesses(child, call_timeout=0.2))
    t0 = time.monotonic()
    with pytest.raises(HarnessError) as e:
        h.call_tool("qloo_where_popular", {"entity": EID, "within": CHICAGO})
    assert e.value.code == TIMEOUT and time.monotonic() - t0 < 2
    child.reply(server.first, wrap({"results": []}))  # the late answer is dropped by id
    assert h.call_tool("qloo_where_popular", {"entity": EID, "within": CHICAGO}) == FX.where_popular(EID, CHICAGO)
    assert h.ready and h.spawner.count == 1  # one timeout does not restart the process


def test_two_timeouts_in_a_row_restart_the_process(harnesses):
    class Mute(FixtureServer):
        def handle(self, msg, child):
            return None if msg.get("method") == "tools/call" else super().handle(msg, child)

    h = ready(harnesses(FakeChild(Mute()), FakeChild(), call_timeout=0.1))
    for _ in range(2):
        with pytest.raises(HarnessError):
            h.call_tool("qloo_where_popular", {})
    assert wait_for(lambda: h.spawner.count == 2 and h.ready)
    assert h.call_tool("qloo_where_popular", {"entity": EID, "within": CHICAGO})


def test_initialize_that_never_answers_counts_as_a_failed_start(harnesses):
    class Dead(FixtureServer):
        def handle(self, msg, child):
            return None

    mk = lambda: FakeChild(Dead())
    h = ready(harnesses(mk, mk, mk, mk, startup_timeout=0.05))
    assert h.status == "unavailable" and h.spawner.count == 4


# ---------------------------------------------------------------- restart and give up


def test_a_process_that_exits_mid_run_is_restarted_with_backoff(harnesses):
    sleeps = []

    class Dies(FixtureServer):
        def handle(self, msg, child):
            if msg.get("method") == "tools/call":
                child.exit()  # the process dies while a call is in flight
                return None
            return super().handle(msg, child)

    h = ready(harnesses(FakeChild(Dies()), FakeChild(), sleep=sleeps.append))
    with pytest.raises(HarnessError) as e:
        h.call_tool("qloo_where_popular", {"entity": EID, "within": CHICAGO})
    assert e.value.code == UNAVAILABLE  # the current call fails with a fixed code
    # the next call waits for the restart instead of failing, then succeeds
    assert h.call_tool("qloo_where_popular", {"entity": EID, "within": CHICAGO}) == FX.where_popular(EID, CHICAGO)
    assert sleeps == [1.0] and h.spawner.count == 2


def test_after_three_restarts_the_harness_is_unavailable(harnesses):
    sleeps = []

    def dies_after_handshake():
        child = FakeChild()
        orig = child.server.handle

        def handle(msg, c):
            result = orig(msg, c)
            if msg.get("method") == "tools/list":
                threading.Timer(0.02, child.exit).start()
            return result

        child.server.handle = handle
        return child

    h = ready(harnesses(*[dies_after_handshake] * 4, sleep=sleeps.append))
    assert wait_for(lambda: h.status == "unavailable")
    assert h.spawner.count == 4  # the first start plus 3 restarts, no more
    assert sleeps == [1.0, 2.0, 4.0]
    with pytest.raises(HarnessError) as e:
        h.call_tool("qloo_where_popular", {})
    assert e.value.code == UNAVAILABLE


def test_a_spawn_failure_is_a_failed_start_not_a_crash(harnesses):
    h = ready(harnesses())  # nothing to spawn: every attempt raises
    assert h.status == "unavailable" and h.spawner.count == 4


def test_a_good_call_resets_the_restart_count(harnesses):
    class DiesOnSecondCall(FixtureServer):
        def handle(self, msg, child):
            if msg.get("method") == "tools/call":
                child.n = getattr(child, "n", 0) + 1
                if child.n == 2:
                    child.exit()
                    return None
            return super().handle(msg, child)

    h = ready(harnesses(*[lambda: FakeChild(DiesOnSecondCall())] * 7))
    args = {"entity": EID, "within": CHICAGO}
    for _ in range(5):  # more crashes than the limit of 3, but a good answer came between each pair
        assert h.call_tool("qloo_where_popular", args)
        with pytest.raises(HarnessError):
            h.call_tool("qloo_where_popular", args)
        assert h.status != "unavailable"


# -------------------------------------------------------------------------- mapping


def one_fixture_entities(path):
    return json.loads((SYNTH_DIR / JUNE / path).read_text(encoding="utf-8"))


def test_where_popular_maps_to_the_shape_ranking_reads(tmp_path):
    doc = FX.where_popular(EID, CHICAGO)
    mapped = mc.map_where_popular({k: v for k, v in doc.items() if k not in ("status", "interpretation", "schema_version")}, CHICAGO)
    assert set(mapped) == {"operation", "status", "interpretation", "results"}
    assert mapped["interpretation"] == {"within": CHICAGO} and mapped["status"] == "ok"
    p = tmp_path / "where_popular_chicago.json"
    p.write_text(json.dumps(mapped))
    ref = tmp_path / "ref.json"
    ref.write_text(json.dumps(doc))
    assert _load_city(p) == _load_city(ref)  # same city, affinity, popularity and cell count


def test_where_popular_without_numbers_fails_instead_of_guessing():
    with pytest.raises(HarnessError) as e:
        mc.map_where_popular({"results": [{"location": {}, "query": {"popularity": 0.5}}]}, CHICAGO)
    assert e.value.code == BAD_RESPONSE
    with pytest.raises(HarnessError):
        mc.map_where_popular({"operation": "where_popular"}, CHICAGO)  # no list of cells
    with pytest.raises(HarnessError):
        mc.map_where_popular({"status": "needs_input", "results": []}, CHICAGO)


@pytest.mark.parametrize("name,target", [("places_chicago.json", "place"), ("brands.json", "brand")])
def test_places_and_brands_map_to_the_same_entities_the_one_off_client_returns(name, target):
    items = one_fixture_entities(name)
    out = mc.map_entities(entities_doc(items))
    assert out == items


def test_similar_maps_and_people_never_keep_tags():
    items = one_fixture_entities("openers.json")
    assert mc.map_entities(entities_doc(items), person=True) == items
    tagged = [{**items[0], "tags": [{"name": "Some Wikipedia category"}]}]  # no subtype: still a person here
    assert "tags" not in mc.map_entities(entities_doc(tagged), person=True)[0]


def test_entities_accept_the_entities_key_and_camel_case_ids_and_flat_affinity():
    doc = {"entities": [{"name": "Invented Room", "entityId": "00000000-0000-4000-8000-000000000077", "popularity": 0.4, "affinity": 0.9}]}
    assert mc.map_entities(doc) == [
        {"name": "Invented Room", "entity_id": "00000000-0000-4000-8000-000000000077", "popularity": 0.4, "query": {"affinity": 0.9}}
    ]


def test_entities_missing_a_needed_field_are_dropped_and_all_missing_fails():
    good = one_fixture_entities("brands.json")[0]
    assert mc.map_entities(entities_doc([{"name": "No Affinity", "entity_id": "x", "popularity": 0.1}, good])) == [good]
    with pytest.raises(HarnessError) as e:
        mc.map_entities(entities_doc([{"name": "No Affinity", "entity_id": "x", "popularity": 0.1}]))
    assert e.value.code == BAD_RESPONSE
    assert mc.map_entities(entities_doc([])) == []  # a city with no venues is not an error
    with pytest.raises(HarnessError):
        mc.map_entities({"operation": "recommend"})


def test_client_sends_the_verified_argument_shapes(harnesses):
    server = FixtureServer()
    h = ready(harnesses(FakeChild(server)))
    c = McpClient(h)
    c.where_popular(EID, CHICAGO)
    c.places(EID, CHICAGO, TAG)
    c.brands(EID)
    c.similar(EID)
    assert server.calls == [
        ("qloo_where_popular", {"entity": EID, "within": CHICAGO}),
        ("qloo_recommend", {"signals": [EID], "target_type": "place", "filter_location": CHICAGO, "include_tags": [TAG], "limit": 10}),
        ("qloo_recommend", {"signals": [EID], "target_type": "brand", "limit": 10}),
        ("qloo_recommend", {"signals": [EID], "target_type": "person", "limit": 10}),
    ]


def test_mcp_client_output_equals_fixture_client_output(harnesses):
    h = ready(harnesses(FakeChild()))
    c = McpClient(h)
    assert c.places(EID, CHICAGO, TAG) == FX.places(EID, CHICAGO, TAG)
    assert c.brands(EID) == FX.brands(EID)
    assert c.similar(EID) == FX.similar(EID)
    got = c.where_popular(EID, CHICAGO)
    assert [x["query"] for x in got["results"]] == [x["query"] for x in FX.where_popular(EID, CHICAGO)["results"]]


def test_search_person_stays_on_the_one_off_client(harnesses):
    class Search(OnlySearch):
        def search_person(self, name):
            return [{"name": name}]

    h = harnesses(FakeChild())
    assert McpClient(h, search_client=Search()).search_person("X") == [{"name": "X"}]
    with pytest.raises(HarnessError):
        McpClient(h).search_person("X")  # no one-off client given: a fixed error, never a guess


def test_only_timeouts_are_retried_and_each_retry_goes_through_the_sleep_hook():
    class Flaky:
        def __init__(self, errors):
            self.errors, self.n = errors, 0

        def call_tool(self, tool, args):
            self.n += 1
            if self.errors:
                raise HarnessError(self.errors.pop(0))
            return {"results": []}

    slept = []
    flaky = Flaky([TIMEOUT])
    McpClient(flaky, sleep=slept.append).brands(EID)
    assert flaky.n == 2 and slept == [2.0]
    tool = Flaky([TOOL_ERROR])
    with pytest.raises(HarnessError):
        McpClient(tool, sleep=slept.append).brands(EID)
    assert tool.n == 1  # a tool error is not retried
    slow = Flaky([TIMEOUT] * 5)
    with pytest.raises(HarnessError):
        McpClient(slow, sleep=slept.append).brands(EID)
    assert slow.n == 2  # bounded: one retry


# -------------------------------------------------- the service on top of the harness


class OnlySearch(QlooClient):
    """A QlooClient that can only search; every other call is a bug in the test."""

    def where_popular(self, entity_id, city):
        raise AssertionError("not used")

    similar = brands = person_description = where_popular

    def places(self, entity_id, city, category_tag):
        raise AssertionError("not used")


class StubSearch(OnlySearch):
    def search_person(self, name):
        return [{"name": f"Test Comic {i}", "entity_id": IDS[i], "popularity": 0.8, "properties": {"short_description": "Stand-Up Comedian"}} for i in range(3)]


def make_app(tmp_path, harness, pacer=None, **overrides):
    overrides.setdefault("max_qloo_per_second", 0)
    settings = Settings(live=True, data_dir=tmp_path / "data", work_dir=tmp_path / "work", **overrides)
    now = lambda: datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    app = create_app(
        settings,
        client_factory=lambda sleep: McpClient(harness, search_client=StubSearch(), sleep=sleep),
        now=now,
        pacer=pacer,
        harness=harness,
    )
    return TestClient(app)


def run_plan(c):
    assert c.post("/api/live/search", json={"name": "Test Comic"}).status_code == 200
    r = c.post("/api/live/plan", json={"entity_id": IDS[0]})
    assert r.status_code == 202, r.text
    text = c.get(r.json()["stream"]).text
    out = []
    for block in text.strip().split("\n\n"):
        f = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in f:
            out.append((f["event"], json.loads(f["data"])))
    return out


def test_a_full_live_run_through_the_persistent_harness(tmp_path, harnesses):
    server = FixtureServer()
    h = ready(harnesses(FakeChild(server), max_in_flight=3))
    evs = run_plan(make_app(tmp_path, h))
    assert evs[-1][0] == "result"
    result = evs[-1][1]
    assert result["mode"] == "live" and result["qloo_calls"] == 14 and len(server.calls) == 14
    assert result["warnings"] == []
    # the same plan the fixture client gives for the same six cities
    assert [c["city"] for c in result["plan"]["cities"]] and set(c["city"] for c in result["plan"]["cities"]) <= set(REDUCED_CITIES)


def test_the_call_cap_holds_under_concurrency_with_the_persistent_client(tmp_path, harnesses):
    server = FixtureServer()
    h = ready(harnesses(FakeChild(server), max_in_flight=6))
    c = make_app(tmp_path, h, live_workers=6, max_qloo_calls=9)
    evs = run_plan(c)
    assert evs[-1][0] == "error" and evs[-1][1]["error"] == "call_cap_reached"
    assert len(server.calls) <= 9  # the harness never sees more than the cap


def test_the_default_cap_is_twenty_and_retries_are_charged_to_it(tmp_path, harnesses):
    class AlwaysSilent(FixtureServer):
        def handle(self, msg, child):
            return None if msg.get("method") == "tools/call" else super().handle(msg, child)

    server = AlwaysSilent()
    h = ready(harnesses(FakeChild(server), call_timeout=0.01, max_in_flight=6, max_restarts=0))
    c = make_app(tmp_path, h, live_workers=6, max_qloo_calls=5)
    import roadie.live as live_mod

    orig = live_mod.time.sleep
    live_mod.time.sleep = lambda s: None
    try:
        evs = run_plan(c)
    finally:
        live_mod.time.sleep = orig
    assert evs[-1][0] == "error"
    assert len(server.calls) <= 5  # first tries and retries together stay inside the cap
    assert Settings().max_qloo_calls == 20


def test_every_persistent_call_takes_a_pace_slot(tmp_path, harnesses):
    waits = []

    class Spy:
        interval = 0.25
        lock = threading.Lock()

        def wait(self):
            with self.lock:
                waits.append(1)

    server = FixtureServer()
    h = ready(harnesses(FakeChild(server), max_in_flight=6))
    run_plan(make_app(tmp_path, h, pacer=Spy(), live_workers=6))
    assert len(waits) == len(server.calls) + 1 == 15  # the search plus 14 harness calls


def test_the_real_pacer_spaces_persistent_calls(tmp_path, harnesses):
    slept = []
    pacer = RatePacer(4, lambda: 100.0, slept.append)  # a frozen clock: every call must queue for its own slot
    server = FixtureServer()
    h = ready(harnesses(FakeChild(server), max_in_flight=6))
    run_plan(make_app(tmp_path, h, pacer=pacer, live_workers=6))
    gaps = sorted(slept)
    assert len(gaps) == 14 and all(b - a >= 0.25 - 1e-9 for a, b in zip(gaps, gaps[1:]))


# ------------------------------------------------------------------------ readiness


def test_live_ready_is_false_and_live_answers_503_starting_until_initialize_completes(tmp_path, harnesses):
    gate = threading.Event()

    class Slow(FixtureServer):
        def handle(self, msg, child):
            if msg.get("method") == "initialize":
                gate.wait(10)
            return super().handle(msg, child)

    h = harnesses(FakeChild(Slow()))
    c = make_app(tmp_path, h)
    health = c.get("/api/health").json()
    assert health["live_enabled"] is True and health["live_ready"] is False and health["live_status"] == "starting"
    for method, path, kw in (("post", "/api/live/search", {"json": {"name": "Test Comic"}}), ("post", "/api/live/plan", {"json": {"entity_id": IDS[0]}})):
        r = getattr(c, method)(path, **kw)
        assert r.status_code == 503 and r.json()["error"] == "live_starting", path
        assert "warming up" in r.json()["message"] and r.headers.get("retry-after")
    assert c.get("/api/live/stream/" + "a" * 20).status_code == 404  # streams are not gated on readiness
    gate.set()
    assert wait_for(lambda: c.get("/api/health").json()["live_ready"] is True)
    assert c.post("/api/live/search", json={"name": "Test Comic"}).status_code == 200


def test_after_giving_up_live_reports_qloo_unavailable_and_the_gallery_still_works(tmp_path, harnesses):
    gallery = tmp_path / "data" / "gallery"
    gallery.mkdir(parents=True)
    (gallery / "alpha.json").write_text(json.dumps({"slug": "alpha", "name": "Alpha", "plan": {}, "built_with": "template"}))
    h = ready(harnesses())  # every spawn fails
    c = make_app(tmp_path, h)
    health = c.get("/api/health").json()
    assert health["live_ready"] is False and health["live_status"] == "unavailable" and health["gallery_count"] == 1
    for path, body in (("/api/live/search", {"name": "Test Comic"}), ("/api/live/plan", {"entity_id": IDS[0]})):
        r = c.post(path, json=body)
        assert r.status_code == 503 and r.json()["error"] == "qloo_unavailable"
    assert c.get("/api/gallery").status_code == 200 and [g["slug"] for g in c.get("/api/gallery").json()] == ["alpha"]
    assert c.get("/api/plan/alpha").status_code == 200
    assert c.get("/").status_code == 200


def test_lifespan_starts_the_harness_when_live_is_on_and_stops_it(tmp_path, harnesses):
    child = FakeChild()
    h = harnesses(child)
    with make_app(tmp_path, h) as c:
        assert wait_for(lambda: c.get("/api/health").json()["live_ready"])
    assert child.killed and h.spawner.count == 1


def test_live_off_never_starts_the_harness(tmp_path, harnesses):
    h = harnesses(FakeChild())
    app = create_app(Settings(live=False, data_dir=tmp_path), harness=h)
    with TestClient(app) as c:
        c.get("/api/health")
        c.post("/api/live/search", json={"name": "Test Comic"})
    assert h.spawner.count == 0


# --------------------------------------------------------------------------- modes


def test_oneshot_mode_still_uses_the_old_client(tmp_path):
    one = Settings(live=True, qloo_mode="oneshot", data_dir=tmp_path)
    svc = LiveService(one)
    assert svc.harness is None and svc.readiness() == "ready"
    assert isinstance(default_client_factory(one)(lambda s: None), LiveClient)


def test_persistent_mode_builds_a_harness_and_an_mcp_client(tmp_path):
    s = Settings(live=True, data_dir=tmp_path)
    assert s.qloo_mode == "persistent"
    svc = LiveService(s)
    assert isinstance(svc.harness, PersistentHarness) and svc.harness.status == "starting"  # not started yet: no process
    client = default_client_factory(s, svc.harness)(lambda sec: None)
    assert isinstance(client, McpClient)
    assert isinstance(client.search_client, LiveClient) and client.search_client.retries == 0 and client.search_client.timeout == 90.0


def test_settings_read_qloo_mode_with_persistent_as_the_default():
    assert Settings.from_env({}).qloo_mode == "persistent"
    assert Settings.from_env({"ROADIE_QLOO_MODE": "oneshot"}).qloo_mode == "oneshot"
    assert Settings.from_env({"ROADIE_QLOO_MODE": " Persistent "}).qloo_mode == "persistent"
    assert Settings.from_env({"ROADIE_QLOO_MODE": "weird"}).qloo_mode == "persistent"


def test_an_injected_client_factory_means_no_harness(tmp_path):
    svc = LiveService(Settings(live=True, data_dir=tmp_path), client_factory=lambda sleep: StubSearch())
    assert svc.harness is None


# -------------------------------------------------------------------- secrets, logs


def test_subprocess_child_runs_qloo_mcp_with_the_inherited_environment(monkeypatch):
    seen = {}

    class P:
        stdin = stdout = None

        def __init__(self, argv, **kw):
            seen["argv"], seen["kw"] = argv, kw

    monkeypatch.setattr(subprocess, "Popen", P)
    mc.SubprocessChild("qloo-bin")
    assert seen["argv"] == ["qloo-bin", "mcp"]
    assert seen["kw"]["env"] is None and seen["kw"]["stderr"] == subprocess.DEVNULL  # nothing is read from or put into the env


def test_the_key_environment_and_command_line_never_reach_a_response_or_a_log(tmp_path, harnesses, monkeypatch, caplog):
    for var in ("QLOO_API_KEY", "OPENAI_API_KEY", "QLOO_BASE_URL", "ROADIE_SOMETHING"):
        monkeypatch.setenv(var, CANARY)
    caplog.set_level(logging.DEBUG)

    class Leaky(FixtureServer):
        def tool_result(self, name, args):
            if name == "qloo_where_popular":
                return tool_error(f"boom {CANARY} qloo mcp --input")
            return wrap({"results": [{"name": CANARY, "entity_id": CANARY}]})  # unusable: no popularity or affinity

    h = ready(harnesses(FakeChild(Leaky())))
    c = make_app(tmp_path, h)
    texts = [c.get("/api/health").text, c.post("/api/live/search", json={"name": "Test Comic"}).text]
    r = c.post("/api/live/plan", json={"entity_id": IDS[0]})
    texts.append(r.text)
    texts.append(c.get(r.json()["stream"]).text)
    for t in texts:
        assert CANARY not in t and "qloo mcp" not in t and "--input" not in t
    server_log = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("roadie"))
    assert CANARY not in server_log and "qloo mcp" not in server_log and "--input" not in server_log


def test_a_dead_harness_during_a_run_ends_with_a_fixed_error(tmp_path, harnesses, caplog):
    caplog.set_level(logging.DEBUG)
    h = ready(harnesses(max_restarts=0))  # unavailable from the start
    h._state = "ready"  # a run that began while it was healthy
    c = make_app(tmp_path, h)
    evs = run_plan(c)
    assert evs[-1][0] == "error" and evs[-1][1]["error"] == "qloo_unavailable"
    assert CANARY not in json.dumps(evs)
