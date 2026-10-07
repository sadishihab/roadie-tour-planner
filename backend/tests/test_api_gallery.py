"""Gallery endpoints, security headers, CORS and settings. No network, no subprocess."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roadie.api import create_app
from roadie.settings import Settings
from synthetic import JUNE, SLUGS, SYNTH_DIR


def write_entry(directory, slug, tiers=("strong", "weaker"), built_with="template", comics=(True, None)):
    entry = {
        "slug": slug,
        "name": slug.title(),
        "description": "Stand-Up Comedian",
        "plan": {
            "cities": [{"city": f"C{i}", "tier": t} for i, t in enumerate(tiers)],
            "comics_to_bill": {"items": [{"identified_as_comedian": v} for v in comics]},
        },
        "narration": {"summary": "s", "source": built_with},
        "built_with": built_with,
        "built_at": "2026-10-06T00:00:00+00:00",
    }
    (directory / f"{slug}.json").write_text(json.dumps(entry))
    return entry


@pytest.fixture
def gallery(tmp_path):
    d = tmp_path / "data" / "gallery"
    d.mkdir(parents=True)
    write_entry(d, "alpha", tiers=("strong", "strong", "weaker"), comics=(True, True, None, False))
    write_entry(d, "beta_2", built_with="openai")
    (d / "broken.json").write_text("{not json")
    (d / "Bad-Name.json").write_text("{}")
    (d / "notes.txt").write_text("x")
    return d


@pytest.fixture
def client(gallery):
    return TestClient(create_app(Settings(data_dir=gallery.parent)))


def test_health_defaults_to_live_off(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "live_enabled", "live_budget_remaining", "gallery_count"}
    assert (body["status"], body["live_enabled"], body["live_budget_remaining"]) == ("ok", False, None)
    assert isinstance(body["gallery_count"], int) and body["gallery_count"] >= 2


def test_health_gallery_count_is_zero_without_gallery_files(tmp_path):
    c = TestClient(create_app(Settings(data_dir=tmp_path)))  # no gallery folder at all
    assert c.get("/api/health").json()["gallery_count"] == 0
    assert c.get("/api/gallery").json() == []


def test_gallery_lists_summaries_and_skips_bad_files(client):
    r = client.get("/api/gallery")
    assert r.status_code == 200
    rows = {x["slug"]: x for x in r.json()}
    assert set(rows) == {"alpha", "beta_2"}
    assert rows["alpha"] == {
        "slug": "alpha", "name": "Alpha", "description": "Stand-Up Comedian", "built_with": "template",
        "strong_city_count": 2, "identified_comics": 2,
    }
    assert rows["beta_2"]["built_with"] == "openai" and rows["beta_2"]["strong_city_count"] == 1


def test_missing_gallery_dir_is_an_empty_list(tmp_path):
    c = TestClient(create_app(Settings(data_dir=tmp_path / "nope")))
    assert c.get("/api/gallery").json() == []
    assert c.get("/api/plan/alpha").status_code == 404


def test_plan_returns_the_gallery_file(client, gallery):
    r = client.get("/api/plan/alpha")
    assert r.status_code == 200
    assert r.json() == json.loads((gallery / "alpha.json").read_text())


def test_unknown_slug_is_404_and_bad_json_file_is_404(client):
    for slug in ("nobody", "broken"):
        r = client.get(f"/api/plan/{slug}")
        assert r.status_code == 404 and r.json()["error"] == "not_found"


@pytest.mark.parametrize(
    "path",
    [
        "/api/plan/../../etc/passwd",
        "/api/plan/..%2f..%2fetc%2fpasswd",
        "/api/plan/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "/api/plan/..%5c..%5cwindows",
        "/api/plan//etc/passwd",
        "/api/plan/%2Fetc%2Fpasswd",
        "/api/plan/Bad-Name",
        "/api/plan/alpha.json",
        "/api/plan/" + "a" * 41,
        "/api/plan/ALPHA",
        "/api/plan/alpha%00",
    ],
)
def test_path_traversal_and_bad_slugs_are_rejected(client, path):
    r = client.get(path)
    assert r.status_code in (404, 422)
    assert "root:" not in r.text


def test_slug_regex_boundaries(gallery):
    write_entry(gallery, "a" * 40)
    c = TestClient(create_app(Settings(data_dir=gallery.parent)))
    assert c.get("/api/plan/" + "a" * 40).status_code == 200
    assert c.get("/api/plan/" + "a" * 41).status_code == 422


def test_security_headers_on_every_response(client):
    for path in ("/api/health", "/api/gallery", "/api/plan/alpha", "/api/plan/nobody", "/nope"):
        h = client.get(path).headers
        assert h["x-content-type-options"] == "nosniff", path
        assert h["referrer-policy"] == "no-referrer", path


def test_cors_only_for_configured_origins(gallery):
    off = TestClient(create_app(Settings(data_dir=gallery.parent)))
    assert "access-control-allow-origin" not in off.get("/api/health", headers={"Origin": "https://a.example"}).headers
    on = TestClient(create_app(Settings(data_dir=gallery.parent, allowed_origins=("https://a.example",))))
    assert on.get("/api/health", headers={"Origin": "https://a.example"}).headers["access-control-allow-origin"] == "https://a.example"
    assert "access-control-allow-origin" not in on.get("/api/health", headers={"Origin": "https://evil.example"}).headers


def test_settings_defaults_and_parsing():
    s = Settings.from_env({})
    assert (s.live, s.allowed_origins, s.trust_proxy, s.ip_runs_per_hour, s.global_runs_per_day) == (False, (), False, 3, 15)
    assert (s.cache_seconds, s.max_qloo_calls, s.max_running_jobs, s.job_ttl_seconds) == (86400, 20, 3, 600)
    assert s.data_dir.name == "data" and s.gallery_dir == s.data_dir / "gallery" and s.fixtures_dir == s.data_dir / "fixtures"
    assert (s.monthly_runs, s.max_qloo_per_second, s.live_until) == (300, 4.0, "")
    assert (s.trusted_proxy_hops, s.live_workers) == (1, 4)
    s = Settings.from_env(
        {
            "ROADIE_LIVE": "1",
            "ROADIE_ALLOWED_ORIGINS": " https://a.example/ , *, ,https://b.example",
            "ROADIE_TRUST_PROXY": "true",
            "ROADIE_IP_RUNS_PER_HOUR": "oops",
            "ROADIE_GLOBAL_RUNS_PER_DAY": "7",
            "ROADIE_MAX_QLOO_CALLS": "100000",
            "ROADIE_DATA_DIR": "/srv/private",
            "ROADIE_MONTHLY_RUNS": "12",
            "ROADIE_MAX_QLOO_PER_SECOND": "9",
            "ROADIE_LIVE_UNTIL": "2026-11-16",
            "ROADIE_TRUSTED_PROXY_HOPS": "2",
        }
    )
    assert s.live and s.trust_proxy and s.trusted_proxy_hops == 2
    assert Settings.from_env({"ROADIE_TRUSTED_PROXY_HOPS": "0"}).trusted_proxy_hops == 1  # at least one hop
    assert Settings.from_env({"ROADIE_TRUSTED_PROXY_HOPS": "x"}).trusted_proxy_hops == 1
    assert s.allowed_origins == ("https://a.example", "https://b.example")
    assert (s.ip_runs_per_hour, s.global_runs_per_day, s.max_qloo_calls) == (3, 7, 100)
    assert str(s.data_dir) == "/srv/private" and str(s.gallery_dir) == "/srv/private/gallery"
    assert (s.monthly_runs, s.max_qloo_per_second, s.live_until) == (12, 5.0, "2026-11-16")  # the pace is capped at the key's 5/s
    assert Settings.from_env({"ROADIE_LIVE_UNTIL": "soon", "ROADIE_MAX_QLOO_PER_SECOND": "nan"}).live_until == ""
    assert Settings.from_env({"ROADIE_MAX_QLOO_PER_SECOND": "nan"}).max_qloo_per_second == 4.0
    assert Settings.from_env({"ROADIE_MAX_QLOO_PER_SECOND": "0"}).max_qloo_per_second == 0.0
    assert Settings.from_env({"ROADIE_GALLERY_DIR": "/srv/g"}).gallery_dir.parent.name == "data"  # the old setting is gone
    assert Settings.from_env({"ROADIE_LIVE": "0"}).live is False


def test_default_data_dir_need_not_exist():
    c = TestClient(create_app(Settings(data_dir=Path("/nonexistent/roadie-data"))))
    assert c.get("/api/gallery").json() == []
    assert c.get("/api/plan/june_marlowe").status_code == 404


def test_gallery_built_from_synthetic_fixtures_is_served(tmp_path, monkeypatch):
    """The whole path with private-data semantics: fixtures in <data>/fixtures, gallery in <data>/gallery."""
    import importlib.util
    import shutil

    spec = importlib.util.spec_from_file_location("build_gallery", SYNTH_DIR.parents[1] / "scripts" / "build_gallery.py")
    bg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bg)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ROADIE_DATA_DIR", str(tmp_path / "data"))
    for slug in SLUGS:
        shutil.copytree(SYNTH_DIR / slug, tmp_path / "data" / "fixtures" / slug)
    assert set(bg.build_gallery().values()) == {"written"}
    c = TestClient(create_app(Settings.from_env()))
    rows = c.get("/api/gallery").json()
    assert {r["slug"] for r in rows} == set(SLUGS)
    assert next(r for r in rows if r["slug"] == JUNE)["strong_city_count"] == 6
    for r in rows:
        data = c.get(f"/api/plan/{r['slug']}").json()
        assert data["built_with"] in ("openai", "template", "template_fallback")
        assert data["plan"]["cities"] and data["narration"]["city_pitches"]
