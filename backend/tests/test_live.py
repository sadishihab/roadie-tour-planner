"""Live mode with a stubbed Qloo client: no network, no real subprocess."""

import json
import logging
import random
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roadie import live as live_mod
from roadie.api import create_app
from roadie.live import REDUCED_CITIES, CallBudget, RatePacer, cap_strings, client_ip
from roadie.qloo_client import FixtureClient, LiveClient, QlooClient, QlooError
from roadie.settings import Settings
from synthetic import JUNE, SYNTH_DIR

CANARY = "canary-secret-7f3a9"
IDS = [f"00000000-0000-4000-8000-{9000 + i:012d}" for i in range(10)]  # synthetic ids


class StubClient(QlooClient):
    """Answers from the synthetic June Marlowe fixtures and counts calls."""

    def __init__(self, shared):
        self.shared = shared
        self.fx = FixtureClient(JUNE, SYNTH_DIR)

    def _count(self, kind):
        with self.shared["lock"]:
            self.shared["calls"].append(kind)
        gate = self.shared.get("gate")
        if gate is not None and kind == "where_popular":
            gate.wait(10)
        hook = self.shared.get("hook")
        if hook:
            hook(kind)

    def search_person(self, name):
        self._count("search")
        if self.shared.get("search_error"):
            raise QlooError(f"qloo search failed (1): bad key {CANARY} --input {{}}")
        return [
            {"name": f"Test Comic {i}", "entity_id": IDS[i], "popularity": 0.8,
             "properties": {"short_description": "Stand-Up Comedian"}}
            for i in range(len(IDS))
        ]

    def where_popular(self, entity_id, city):
        self._count("where_popular")
        return self.fx.where_popular(entity_id, city)

    def similar(self, entity_id):
        self._count("similar")
        return self.fx.similar(entity_id)

    def places(self, entity_id, city, category_tag):
        self._count("places")
        return self.fx.places(entity_id, city, category_tag)

    def brands(self, entity_id):
        self._count("brands")
        data = self.fx.brands(entity_id)
        if self.shared.get("long_brand"):
            data = [{**data[0], "name": "B" * 5000}, *data[1:]]
        return data

    def person_description(self, entity_id):
        raise AssertionError("live runs do no description lookups")


@pytest.fixture
def shared():
    return {"lock": threading.Lock(), "calls": []}


@pytest.fixture
def clock():
    t = [1000.0]

    def now():
        return t[0]

    now.advance = lambda s: t.__setitem__(0, t[0] + s)
    return now


@pytest.fixture
def wall():
    """The wall clock (UTC) behind the monthly budget and ROADIE_LIVE_UNTIL; tests move it by hand."""
    t = [datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)]

    def now():
        return t[0]

    now.set = lambda dt: t.__setitem__(0, dt)
    now.advance = lambda **kw: t.__setitem__(0, t[0] + timedelta(**kw))
    return now


@pytest.fixture
def make(tmp_path, shared, clock, wall):
    def build(pacer=None, **overrides):
        overrides.setdefault("data_dir", tmp_path / "data")  # need not exist
        overrides.setdefault("max_qloo_per_second", 0)  # no real sleeping in tests; pacing has its own tests
        settings = Settings(live=True, work_dir=tmp_path / "work", **overrides)
        app = create_app(settings, client_factory=lambda sleep: StubClient(shared), clock=clock, now=wall, pacer=pacer)
        return TestClient(app)

    return build


def search(c, name="Test Comic", **kw):
    return c.post("/api/live/search", json={"name": name}, **kw)


def plan(c, entity_id, **kw):
    return c.post("/api/live/plan", json={"entity_id": entity_id}, **kw)


def events(c, job_id):
    r = c.get(f"/api/live/stream/{job_id}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    out = []
    for block in r.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


def run(c, i=0, **kw):
    assert search(c).status_code == 200
    r = plan(c, IDS[i], **kw)
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


# ------------------------------------------------------------------ disabled by default


LIVE_REQUESTS = [
    ("post", "/api/live/search", {"json": {"name": "June Marlowe"}}),
    ("post", "/api/live/search", {"json": {"name": "!!"}}),
    ("post", "/api/live/search", {"content": b"not json"}),
    ("post", "/api/live/plan", {"json": {"entity_id": IDS[0]}}),
    ("post", "/api/live/plan", {"json": {"entity_id": "nope"}}),
    ("get", "/api/live/stream/abcdefghijklmnopqrstuv", {}),
    ("get", "/api/live/stream/x", {}),
]


@pytest.mark.parametrize("method,path,kw", LIVE_REQUESTS)
@pytest.mark.parametrize("how", ["default", "flag_without_cli"])
def test_live_disabled_returns_503_everywhere(tmp_path, method, path, kw, how):
    if how == "default":
        settings = Settings(data_dir=tmp_path)
    else:  # ROADIE_LIVE=1 but no qloo CLI on the host (and no injected client)
        settings = Settings(data_dir=tmp_path, live=True, qloo_bin="definitely-not-installed-qloo")
    c = TestClient(create_app(settings))
    health = c.get("/api/health").json()
    assert health["live_enabled"] is False and health["live_budget_remaining"] is None
    r = getattr(c, method)(path, **kw)
    assert r.status_code == 503
    body = r.json()
    assert body["error"] == "live_disabled" and body["message"]


def test_health_reports_live_enabled(make):
    assert make().get("/api/health").json() == {"status": "ok", "live_enabled": True, "live_budget_remaining": 15, "gallery_count": 0}


# ----------------------------------------------------------------------- validation


@pytest.mark.parametrize("name", ["", "a", "x" * 61, "June1", "June<script>", "June; rm -rf", "../etc", "June\nMarlowe", " "])
def test_bad_names_are_422(make, shared, name):
    r = search(make(), name)
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    assert shared["calls"] == []  # nothing ran


@pytest.mark.parametrize("name", ["June Marlowe", "Jo", "Conan O'Brien", "Jean-Luc Ng", "Bo Burnham Jr.", "x" * 59 + "y"])
def test_good_names_are_accepted(make, name):
    assert search(make(), name).status_code == 200


def test_search_body_shapes(make):
    c = make()
    for bad in ({}, {"name": 5}, {"name": None}, [], "x"):
        assert c.post("/api/live/search", json=bad).status_code == 422
    assert c.post("/api/live/search", content=b"{" * 10).status_code == 422
    assert c.post("/api/live/search", content=b"x" * 5000).status_code == 413


@pytest.mark.parametrize(
    "entity_id",
    ["", "nope", "../../etc/passwd", "/etc/passwd", IDS[0] + "0", IDS[0][:-1], "g" * 8 + IDS[0][8:], IDS[0] + "\n"],
)
def test_bad_entity_ids_are_422(make, shared, entity_id):
    r = plan(make(), entity_id)
    assert r.status_code == 422
    assert shared["calls"] == []


def test_plan_requires_a_prior_search(make, shared):
    r = plan(make(), IDS[0])
    assert r.status_code == 404 and r.json()["error"] == "unknown_entity"
    assert shared["calls"] == []


def test_search_returns_candidates_only_and_runs_nothing_expensive(make, shared):
    r = search(make())
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) <= 5 and shared["calls"] == ["search"]
    assert set(rows[0]) == {"entity_id", "name", "description", "popularity"}
    assert rows[0]["description"] == "Stand-Up Comedian"  # echoed from the stub


def test_unknown_or_malformed_job_ids_are_404(make):
    c = make()
    assert c.get("/api/live/stream/abcdefghijklmnopqrstuvwx").status_code == 404
    assert c.get("/api/live/stream/short").status_code == 404
    assert c.get("/api/live/stream/..%2f..%2fetc%2fpasswd").status_code in (404, 422)


# --------------------------------------------------------------------------- the run


def test_reduced_run_streams_steps_then_a_template_plan(make, shared, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", CANARY)  # live mode must never use it
    seen = {}

    def hook(kind):
        if kind == "brands":  # last call: inspect the temporary folder mid-run
            folders = list((tmp_path / "work").iterdir())
            run_dir = list(folders[0].iterdir())[0]
            seen["files"] = sorted(p.name for p in run_dir.iterdir())
            seen["chosen"] = json.loads((run_dir / "chosen.json").read_text())

    shared["hook"] = hook
    c = make()
    evs = events(c, run(c))
    names = [e for e, _ in evs]
    assert names[:4] == ["started"] * 4
    assert [d["group"] for e, d in evs[:4]] == ["where_popular", "places", "similar_comics", "brands"]
    steps = [d["step"] for e, d in evs if e == "step"]
    assert steps == ["resolve_comedian", "rank_cities", "route_suggestion", "find_comics_to_bill", "comedy_venues", "sponsor_candidates"]
    assert names[-1] == "result" and names.count("result") == 1
    result = evs[-1][1]
    assert result["mode"] == "live" and result["narration"]["source"] == "template"
    assert result["plan"]["comedian"]["name"] == "Test Comic 0"
    assert [x["city"] for x in result["plan"]["cities"]] and {x["city"] for x in result["plan"]["cities"]} == set(REDUCED_CITIES)
    assert result["narration"]["city_pitches"]
    # 6 where_popular + 6 places + similar + brands, never more than the cap
    assert len(REDUCED_CITIES) == 6
    calls = [k for k in shared["calls"] if k != "search"]
    assert len(calls) == result["qloo_calls"] == 14 <= 20
    assert calls.count("where_popular") == 6 and calls.count("places") == 6 and calls.count("similar") == calls.count("brands") == 1
    # temp folder layout during the run, and it is gone afterwards
    assert "chosen.json" in seen["files"] and "search.json" in seen["files"] and "descriptions.json" not in seen["files"]
    assert set(seen["chosen"]) == {"entity_id", "name", "description"}  # description only, never tags
    assert list((tmp_path / "work").iterdir()) == []


def test_temp_folder_is_deleted_on_failure(make, shared, tmp_path):
    def hook(kind):
        if kind == "brands":
            raise RuntimeError(f"boom {CANARY}")

    shared["hook"] = hook
    c = make()
    evs = events(c, run(c))
    # a brands failure is soft: the plan still comes back, flagged
    assert evs[-1][0] == "result" and "brands_failed" in evs[-1][1]["warnings"]
    shared["hook"] = lambda kind: (_ for _ in ()).throw(RuntimeError("x")) if kind == "similar" else None
    c2 = make()
    assert events(c2, run(c2, 1))[-1][0] == "result"
    assert list((tmp_path / "work").iterdir()) == []


def test_all_where_popular_failing_is_a_fixed_error(make, shared, tmp_path):
    def hook(kind):
        if kind == "where_popular":
            raise QlooError(f"qloo where_popular failed (1): {CANARY}")

    shared["hook"] = hook
    c = make()
    last = events(c, run(c))[-1]
    assert last == ("error", {"error": "qloo_unavailable", "message": live_mod.MESSAGES["qloo_unavailable"]})
    assert list((tmp_path / "work").iterdir()) == []


def test_per_run_call_cap_is_hard(make, shared, tmp_path):
    c = make(max_qloo_calls=5)
    last = events(c, run(c))[-1]
    assert last[0] == "error" and last[1]["error"] == "call_cap_reached"
    assert len([k for k in shared["calls"] if k != "search"]) <= 5
    assert list((tmp_path / "work").iterdir()) == []


def test_retries_are_charged_to_the_cap(tmp_path, shared, clock, monkeypatch, caplog):
    attempts = []

    def fake_run(argv, **kw):
        attempts.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", f"error: key {CANARY}")

    monkeypatch.setattr("roadie.qloo_client.subprocess.run", fake_run)
    monkeypatch.setattr(live_mod.time, "sleep", lambda s: None)
    settings = Settings(data_dir=tmp_path, live=True, work_dir=tmp_path / "w", max_qloo_calls=5, max_qloo_per_second=0)
    c = TestClient(create_app(settings, client_factory=lambda sleep: LiveClient(sleep=sleep), clock=clock))
    caplog.set_level(logging.DEBUG)
    search_resp = c.post("/api/live/search", json={"name": "June Marlowe"})
    assert search_resp.status_code == 502 and search_resp.json()["error"] == "qloo_unavailable"
    n_search = len(attempts)
    assert n_search == 3  # LiveClient's own bound: 1 + 2 retries
    # Make the plan reachable by seeding a candidate directly, then run with the failing client.
    svc = c.app.state.live_service
    svc._candidates[IDS[0]] = (clock(), {"entity_id": IDS[0], "name": "Test Comic", "description": None, "popularity": 0.8})
    r = plan(c, IDS[0])
    last = events(c, r.json()["job_id"])[-1]
    assert last[1]["error"] == "call_cap_reached"
    assert len(attempts) - n_search <= 5
    server_log = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("roadie"))
    assert CANARY not in r.text and CANARY not in server_log and "--input" not in server_log


# ---------------------------------------------------------------------------- limits


def test_per_ip_limit_is_three_per_hour_then_recovers(make, shared, clock):
    c = make()
    for i in range(3):
        events(c, run(c, i))
    r = plan(c, IDS[3])
    assert r.status_code == 429 and r.json()["error"] == "ip_limit" and int(r.headers["retry-after"]) > 0
    clock.advance(3601)
    assert plan(c, IDS[3]).status_code == 202


def test_rejected_requests_do_not_start_runs(make, shared):
    c = make(ip_runs_per_hour=1)
    events(c, run(c, 0))
    before = len(shared["calls"])
    assert plan(c, IDS[1]).status_code == 429
    assert len(shared["calls"]) == before


def test_global_daily_limit_across_ips(make, shared, clock):
    c = make(global_runs_per_day=2, trust_proxy=True)
    search(c)
    for i, ip in enumerate(["10.0.0.1", "10.0.0.2"]):
        assert plan(c, IDS[i], headers={"X-Forwarded-For": ip}).status_code == 202
    r = plan(c, IDS[2], headers={"X-Forwarded-For": "10.0.0.3"})
    assert r.status_code == 429 and r.json()["error"] == "daily_limit"
    clock.advance(86401)
    search(c)
    assert plan(c, IDS[2], headers={"X-Forwarded-For": "10.0.0.3"}).status_code == 202


def test_spoofed_forwarded_for_is_ignored_unless_trusted(make):
    c = make()  # trust_proxy off: the direct peer counts
    search(c)
    for i in range(3):
        assert plan(c, IDS[i], headers={"X-Forwarded-For": f"9.9.9.{i}"}).status_code == 202
    assert plan(c, IDS[3], headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 429


def test_trusted_proxy_ignores_a_spoofed_leading_value(make):
    c = make(trust_proxy=True, max_running_jobs=10)  # 3 runs per IP per hour, one trusted hop
    search(c)
    for i in range(3):  # the client varies the leading value; the proxy-appended last entry is what counts
        assert plan(c, IDS[i], headers={"X-Forwarded-For": f"9.9.9.{i}, 8.8.8.8"}).status_code == 202
    r = plan(c, IDS[3], headers={"X-Forwarded-For": "7.7.7.7, 8.8.8.8"})
    assert r.status_code == 429 and r.json()["error"] == "ip_limit"


def test_client_ip_rules():
    assert client_ip("1.2.3.4", "8.8.8.8", False) == "1.2.3.4"
    assert client_ip("1.2.3.4", "8.8.8.8", True) == "8.8.8.8"
    assert client_ip("1.2.3.4", None, True) == "1.2.3.4"
    assert client_ip(None, None, False) == "unknown"


def test_client_ip_counts_trusted_hops_from_the_right():
    assert client_ip("1.2.3.4", "6.6.6.6, 8.8.8.8", True) == "8.8.8.8"  # spoofed leading value ignored
    assert client_ip("1.2.3.4", "6.6.6.6, 8.8.8.8", True, 1) == "8.8.8.8"  # a single hop reads the last entry
    assert client_ip("1.2.3.4", "6.6.6.6, 8.8.8.8, 10.0.0.9", True, 2) == "8.8.8.8"  # two hops: second from the end
    assert client_ip("1.2.3.4", "5.5.5.5, 6.6.6.6, 8.8.8.8", True, 3) == "5.5.5.5"
    assert client_ip("1.2.3.4", " 2001:db8::1 ", True) == "2001:db8::1"


def test_client_ip_falls_back_to_the_direct_peer_when_the_header_is_short_or_malformed():
    assert client_ip("1.2.3.4", "8.8.8.8", True, 2) == "1.2.3.4"  # fewer entries than hops
    assert client_ip("1.2.3.4", "not-an-ip", True) == "1.2.3.4"
    assert client_ip("1.2.3.4", "8.8.8.8, not-an-ip", True) == "1.2.3.4"  # the counted entry is unusable
    assert client_ip("1.2.3.4", "8.8.8.8,", True) == "1.2.3.4"  # empty last entry; never reaches back to a client value
    assert client_ip("1.2.3.4", ",,", True) == "1.2.3.4"
    assert client_ip("1.2.3.4", "8.8.8.8", True, 0) == "1.2.3.4"
    assert client_ip("1.2.3.4", "8.8.8.8", False, 1) == "1.2.3.4"
    assert client_ip(None, "x", True) == "unknown"


def test_two_hop_setting_reaches_the_api(make):
    c = make(trust_proxy=True, trusted_proxy_hops=2, max_running_jobs=10)
    search(c)
    for i in range(3):  # entries: spoofed, client as the first proxy saw it, second proxy's peer
        assert plan(c, IDS[i], headers={"X-Forwarded-For": f"9.9.9.{i}, 8.8.8.8, 10.0.0.1"}).status_code == 202
    r = plan(c, IDS[3], headers={"X-Forwarded-For": "1.1.1.1, 8.8.8.8, 10.0.0.2"})
    assert r.status_code == 429 and r.json()["error"] == "ip_limit"


def test_at_most_three_running_a_fourth_gets_429(make, shared):
    gate = threading.Event()
    shared["gate"] = gate
    c = make(ip_runs_per_hour=50, global_runs_per_day=50)
    try:
        search(c)
        ids = [plan(c, IDS[i]).json()["job_id"] for i in range(3)]
        r = plan(c, IDS[3])
        assert r.status_code == 429 and r.json()["error"] == "too_many_jobs"
        gate.set()
        for jid in ids:
            assert events(c, jid)[-1][0] == "result"
        for _ in range(100):  # the worker frees its slot just after the last event
            if c.app.state.live_service._running == 0:
                break
            threading.Event().wait(0.02)
        assert plan(c, IDS[3]).status_code == 202
    finally:
        gate.set()


def test_same_entity_in_flight_shares_one_job(make, shared):
    gate = threading.Event()
    shared["gate"] = gate
    c = make()
    try:
        search(c)
        a, b = plan(c, IDS[0]).json(), plan(c, IDS[0]).json()
        assert a["job_id"] == b["job_id"] and b["reused"] is True
        gate.set()
        assert events(c, a["job_id"])[-1][0] == "result"
    finally:
        gate.set()
    assert shared["calls"].count("brands") == 1


def test_cache_returns_previous_result_without_new_calls_for_24h(make, shared, clock):
    c = make(ip_runs_per_hour=1)
    first = events(c, run(c))
    n = len(shared["calls"])
    clock.advance(23 * 3600)
    r = plan(c, IDS[0])  # also does not use the (exhausted) per-IP quota
    assert r.status_code == 202 and r.json()["reused"] is True
    again = events(c, r.json()["job_id"])
    assert again == first and len(shared["calls"]) == n
    clock.advance(2 * 3600)  # now past 24 hours
    search(c)
    r = plan(c, IDS[0])
    assert r.status_code == 202 and r.json()["reused"] is False
    assert events(c, r.json()["job_id"])[-1][0] == "result" and len(shared["calls"]) > n


def test_failed_runs_are_not_cached(make, shared):
    shared["hook"] = lambda kind: (_ for _ in ()).throw(QlooError("x")) if kind == "where_popular" else None
    c = make()
    assert events(c, run(c))[-1][0] == "error"
    shared["hook"] = None
    r = plan(c, IDS[0])
    assert r.json()["reused"] is False and events(c, r.json()["job_id"])[-1][0] == "result"


def test_jobs_expire_after_ten_minutes(make, clock):
    c = make()
    jid = run(c)
    events(c, jid)
    clock.advance(599)
    assert c.get(f"/api/live/stream/{jid}").status_code == 200
    clock.advance(2)
    assert c.get(f"/api/live/stream/{jid}").status_code == 404


def test_search_is_rate_limited(make):
    c = make(ip_searches_per_hour=2)
    assert search(c).status_code == 200 and search(c).status_code == 200
    r = search(c)
    assert r.status_code == 429 and r.json()["error"] == "search_limit"


# ------------------------------------------------------------- secrets and untrusted text


def test_no_response_or_log_contains_the_canary(make, shared, caplog, monkeypatch):
    for var in ("OPENAI_API_KEY", "QLOO_API_KEY", "ROADIE_MODEL"):
        monkeypatch.setenv(var, CANARY)
    caplog.set_level(logging.DEBUG)
    texts = []
    c = make()
    shared["search_error"] = True
    r = search(c)
    assert r.status_code == 502 and r.json()["error"] == "qloo_unavailable"
    texts.append(r.text)
    shared["search_error"] = False
    shared["hook"] = lambda kind: (_ for _ in ()).throw(QlooError(f"{CANARY} qloo exec --input")) if kind in ("places", "brands") else None
    jid = run(c)
    texts.append(c.get(f"/api/live/stream/{jid}").text)
    shared["hook"] = lambda kind: (_ for _ in ()).throw(QlooError(CANARY)) if kind == "where_popular" else None
    jid = run(c, 1)
    texts.append(c.get(f"/api/live/stream/{jid}").text)
    for path in ("/api/health", "/api/gallery", "/api/plan/june_marlowe", "/api/plan/%s" % CANARY, "/nope"):
        texts.append(c.get(path).text)
    texts.append(c.post("/api/live/search", json={"name": CANARY}).text)
    texts.append(c.post("/api/live/plan", json={"entity_id": CANARY}).text)
    for t in texts:
        assert CANARY not in t and "--input" not in t
    # the HTTP test client logs the URLs it was asked for (including the canary one); only the server's logs matter
    server_log = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("roadie"))
    assert CANARY not in server_log and "--input" not in server_log


def test_unhandled_exception_returns_a_fixed_error(make, caplog):
    c = TestClient(make().app, raise_server_exceptions=False)
    c.app.state.live_service.search = lambda ip, name: (_ for _ in ()).throw(RuntimeError(CANARY))
    r = c.post("/api/live/search", json={"name": "June Marlowe"})
    assert r.status_code == 500 and r.json()["error"] == "internal_error"
    server_log = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("roadie"))
    assert CANARY not in r.text and CANARY not in server_log


def test_qloo_text_is_length_capped(make, shared):
    shared["long_brand"] = True
    c = make()
    result = events(c, run(c))[-1][1]
    strings = []

    def walk(v):
        if isinstance(v, str):
            strings.append(v)
        elif isinstance(v, list):
            [walk(x) for x in v]
        elif isinstance(v, dict):
            [walk(x) for x in v.values()]

    walk(result)
    assert max(map(len, strings)) <= live_mod.MAX_RESPONSE_TEXT
    brand = result["plan"]["sponsor_candidates"]["items"][0]["name"]
    assert len(brand) <= live_mod.MAX_TEXT


def test_cap_strings():
    assert cap_strings("a  b\n c") == "a b c"
    assert len(cap_strings("x" * 1000, 50)) == 50
    assert cap_strings({"a": ["y" * 500, 3, None]}, 10) == {"a": ["y" * 9 + "…", 3, None]}


def test_reduced_cities_constant():
    assert len(REDUCED_CITIES) == 6 and len(set(REDUCED_CITIES)) == 6


# ------------------------------------------------- quota: monthly ceiling, pacing, end of the key


def test_new_defaults_match_the_hackathon_quota():
    s = Settings.from_env({})
    assert (s.ip_runs_per_hour, s.global_runs_per_day, s.monthly_runs, s.max_qloo_calls) == (3, 15, 300, 20)
    assert s.max_qloo_per_second == 4.0  # the key allows 5 per second
    assert s.max_qloo_calls * s.monthly_runs <= 10_000  # a month of capped runs fits the monthly quota


def test_health_budget_is_the_smaller_of_daily_and_monthly(make):
    c = make(global_runs_per_day=5, monthly_runs=3, ip_runs_per_hour=50)
    assert c.get("/api/health").json()["live_budget_remaining"] == 3
    events(c, run(c, 0))
    assert c.get("/api/health").json()["live_budget_remaining"] == 2
    c2 = make(global_runs_per_day=2, monthly_runs=300, ip_runs_per_hour=50)
    assert c2.get("/api/health").json()["live_budget_remaining"] == 2


def test_cache_hits_spend_no_budget(make):
    c = make(ip_runs_per_hour=50)
    events(c, run(c, 0))
    before = c.get("/api/health").json()["live_budget_remaining"]
    assert plan(c, IDS[0]).json()["reused"] is True
    assert c.get("/api/health").json()["live_budget_remaining"] == before


def test_monthly_ceiling_then_the_next_month_resets(make, wall):
    c = make(monthly_runs=2, ip_runs_per_hour=50)
    events(c, run(c, 0))
    events(c, plan_ok(c, 1))
    r = plan(c, IDS[2])
    assert r.status_code == 429 and r.json()["error"] == "monthly_limit"
    assert "monthly limit" in r.json()["message"] and "gallery" in r.json()["message"]
    assert c.get("/api/health").json()["live_budget_remaining"] == 0
    wall.set(datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc))
    assert c.get("/api/health").json()["live_budget_remaining"] == 2
    assert plan(c, IDS[2]).status_code == 202


def plan_ok(c, i):
    r = plan(c, IDS[i])
    assert r.status_code == 202, r.text
    return r.json()["job_id"]


def test_monthly_budget_survives_a_restart_when_the_data_dir_exists(make, tmp_path, wall):
    (tmp_path / "data").mkdir()
    c = make(monthly_runs=5, ip_runs_per_hour=50)
    events(c, run(c, 0))
    saved = json.loads((tmp_path / "data" / "live_budget.json").read_text())
    assert saved == {"month": "2026-10", "runs": 1}  # counts only: nothing about visitors
    again = make(monthly_runs=5, ip_runs_per_hour=50)
    assert again.get("/api/health").json()["live_budget_remaining"] == 4
    wall.set(datetime(2026, 12, 1, tzinfo=timezone.utc))
    assert make(monthly_runs=5).get("/api/health").json()["live_budget_remaining"] == 5


def test_nothing_is_written_when_the_data_dir_does_not_exist(make, tmp_path):
    c = make()
    events(c, run(c, 0))
    assert not (tmp_path / "data").exists()


def test_live_degrades_to_a_clear_message_after_the_key_ends(make, wall):
    c = make(live_until="2026-11-16")
    wall.set(datetime(2026, 11, 16, 23, 59, tzinfo=timezone.utc))
    assert c.get("/api/health").json()["live_enabled"] is True  # the last day still works
    wall.set(datetime(2026, 11, 17, 0, 1, tzinfo=timezone.utc))
    health = c.get("/api/health").json()
    assert health == {"status": "ok", "live_enabled": False, "live_budget_remaining": None, "gallery_count": 0}
    for method, path, kw in LIVE_REQUESTS:
        r = getattr(c, method)(path, **kw)
        assert r.status_code == 503 and r.json()["error"] == "live_ended", path
        assert "no longer active" in r.json()["message"] and "gallery" in r.json()["message"]
    assert c.get("/api/gallery").status_code == 200  # the gallery keeps working


def test_qloo_failure_message_is_clear_and_not_a_server_error(make, shared):
    shared["search_error"] = True
    r = search(make())
    assert r.status_code == 502 and r.json()["error"] == "qloo_unavailable"
    assert "gallery" in r.json()["message"] and CANARY not in r.text


def fake_time():
    t = [0.0]
    slept = []

    def clock():
        return t[0]

    def sleep(s):
        slept.append(s)
        t[0] += s

    return t, slept, clock, sleep


def test_pacer_never_exceeds_four_calls_per_second():
    t, slept, clock, sleep = fake_time()
    pacer = RatePacer(4, clock, sleep)
    starts = []
    for _ in range(40):
        pacer.wait()
        starts.append(t[0])
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert min(gaps) >= 0.25
    assert all(sum(1 for s in starts if w <= s < w + 1.0) <= 4 for w in starts)  # any 1 s window


def test_pacer_does_not_delay_a_call_after_an_idle_period():
    t, slept, clock, sleep = fake_time()
    pacer = RatePacer(4, clock, sleep)
    pacer.wait()
    t[0] += 5
    pacer.wait()
    assert slept == []


def test_pacer_off_when_rate_is_zero():
    t, slept, clock, sleep = fake_time()
    pacer = RatePacer(0, clock, sleep)
    for _ in range(10):
        pacer.wait()
    assert slept == []


def test_every_live_qloo_call_takes_a_pace_slot(make, shared):
    waits = []

    class Spy:
        interval = 0.25

        def wait(self):
            waits.append(len(shared["calls"]))

    c = make(pacer=Spy())
    events(c, run(c))
    assert len(waits) == len(shared["calls"]) == 1 + 14  # one search plus a 14-call run


def test_retries_wait_for_a_pace_slot_too(monkeypatch):
    waits = []

    class Spy:
        def wait(self):
            waits.append(1)

    monkeypatch.setattr(live_mod.time, "sleep", lambda s: None)
    budget = CallBudget(5, Spy())
    budget.retry_sleep(2.0)
    assert waits == [1] and budget.count == 1  # charged to the cap and paced


def test_cap_of_twenty_calls_per_run_is_the_default_and_hard(make, shared):
    assert make().app.state.live_service.s.max_qloo_calls == 20
    c = make(max_qloo_calls=20)
    events(c, run(c))
    assert len([k for k in shared["calls"] if k != "search"]) <= 20


# ------------------------------------------------------------------ concurrent workers


def _run_result(c, i=0):
    evs = events(c, run(c, i))
    assert evs[-1][0] == "result", evs[-1]
    return evs, evs[-1][1]


def test_workers_setting_defaults_to_four_and_is_clamped():
    assert Settings.from_env({}).live_workers == 4
    assert Settings.from_env({"ROADIE_LIVE_WORKERS": "1"}).live_workers == 1
    assert Settings.from_env({"ROADIE_LIVE_WORKERS": "99"}).live_workers == 6  # hard maximum
    assert Settings.from_env({"ROADIE_LIVE_WORKERS": "0"}).live_workers == 1
    assert Settings.from_env({"ROADIE_LIVE_WORKERS": "oops"}).live_workers == 4


def test_four_workers_finish_well_under_serial_time(make, shared):
    shared["hook"] = lambda kind: time.sleep(0.1) if kind != "search" else None
    timings = {}
    for workers in (1, 4):
        c = make(live_workers=workers)
        t0 = time.monotonic()
        _run_result(c, 0)
        timings[workers] = time.monotonic() - t0
    assert timings[1] >= 14 * 0.1 * 0.95  # serial: 14 calls in a row
    assert timings[4] < timings[1] * 0.55


def test_concurrency_never_exceeds_the_worker_count(make, shared):
    state = {"now": 0, "peak": 0}

    def hook(kind):
        if kind == "search":
            return
        with shared["lock"]:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.03)
        with shared["lock"]:
            state["now"] -= 1

    shared["hook"] = hook
    _run_result(make(live_workers=3))
    assert 2 <= state["peak"] <= 3


def test_call_cap_is_exact_under_concurrency(make, shared):
    shared["hook"] = lambda kind: time.sleep(0.01)
    c = make(live_workers=6, max_qloo_calls=9)
    evs = events(c, run(c))
    assert evs[-1][0] == "error" and evs[-1][1]["error"] == "call_cap_reached"
    assert len([k for k in shared["calls"] if k != "search"]) == 9  # the 10th is refused before any call starts


def test_call_budget_charge_is_atomic_across_threads():
    budget = CallBudget(20)
    outcomes = []

    def worker():
        for _ in range(50):
            try:
                budget.charge()
                outcomes.append(1)
            except live_mod.BudgetExceeded:
                outcomes.append(0)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(outcomes) == budget.count == 20


def test_pacer_hands_each_concurrent_caller_its_own_slot():
    slept = []
    pacer = RatePacer(4, lambda: 100.0, slept.append)  # the clock never moves: every caller must queue
    threads = [threading.Thread(target=pacer.wait) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    gaps = sorted(slept)
    assert len(gaps) == 11 and all(b - a >= 0.25 - 1e-9 for a, b in zip(gaps, gaps[1:]))  # 4 per second at most
    assert gaps[0] >= 0.25


def test_every_concurrent_call_takes_a_pace_slot(make, shared):
    waits = []

    class Spy:
        interval = 0.25
        lock = threading.Lock()

        def wait(self):
            with self.lock:
                waits.append(1)

    shared["hook"] = lambda kind: time.sleep(0.01)
    _run_result(make(pacer=Spy(), live_workers=6))
    assert len(waits) == len(shared["calls"]) == 15


def test_a_failing_call_is_a_warning_and_the_rest_complete(make, shared):
    seen = {"places": 0}

    def hook(kind):
        if kind == "places":
            with shared["lock"]:
                seen["places"] += 1
                n = seen["places"]
            if n == 3:
                raise QlooError(f"qloo places failed (1): {CANARY}")

    shared["hook"] = hook
    c = make(live_workers=4)
    evs, result = _run_result(c)
    assert [w.split(":")[0] for w in result["warnings"]] == ["places_failed"]
    assert result["qloo_calls"] == 14  # the failed call is still charged
    steps = [d["step"] for e, d in evs if e == "step"]
    assert steps[-1] == "sponsor_candidates" and len(steps) == 6
    assert result["plan"]["cities"] and result["plan"]["sponsor_candidates"]
    assert CANARY not in json.dumps(evs)


def test_concurrent_plan_equals_serial_plan(make, shared):
    def jitter(kind):  # scramble completion order differently on every call
        time.sleep(random.random() * 0.05)

    shared["hook"] = jitter
    random.seed(7)
    serial_evs, serial = _run_result(make(live_workers=1))
    for seed in (1, 2, 3):
        random.seed(seed)
        conc_evs, conc = _run_result(make(live_workers=6))
        assert conc == serial
        assert [(e, d.get("group") or d.get("step")) for e, d in conc_evs] == [(e, d.get("group") or d.get("step")) for e, d in serial_evs]


def test_concurrent_failure_warnings_come_out_in_a_fixed_order(make, shared):
    def hook(kind):
        time.sleep(random.random() * 0.03)
        if kind in ("brands", "similar"):
            raise QlooError("boom")

    shared["hook"] = hook
    _, a = _run_result(make(live_workers=1))
    _, b = _run_result(make(live_workers=6))
    assert a["warnings"] == b["warnings"] == ["similar_comics_failed", "brands_failed"]
