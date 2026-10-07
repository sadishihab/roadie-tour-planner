"""frontend/index.html: static scans, the served page and its CSP, and a synthetic gallery. No network."""

import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roadie.api import CSP, create_app
from roadie.narrator import TemplateNarrator
from roadie.settings import Settings
from synthetic import HARLAN, JUNE, SLUGS, SYNTH_DIR

ROOT = Path(__file__).resolve().parents[2]
PAGE = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
SCRIPT = re.search(r"<script>(.*?)</script>", PAGE, re.S).group(1)

spec = importlib.util.spec_from_file_location("build_gallery", ROOT / "scripts" / "build_gallery.py")
bg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bg)

EXPECTED_CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'"
)


def test_forbidden_html_sinks_are_absent():
    for pattern in (r"innerHTML", r"outerHTML", r"insertAdjacentHTML", r"document\s*\.\s*write", r"\beval\b", r"new\s+Function\b"):
        assert not re.search(pattern, PAGE), pattern


def test_no_external_urls_or_resources():
    assert not re.search(r"https?://", PAGE, re.I)
    assert not re.search(r"<(?:script|link|img|iframe)\b[^>]*\b(?:src|href)\s*=", PAGE, re.I)
    assert not re.search(r"@import|url\(", PAGE, re.I)


def test_every_fetch_and_event_source_targets_the_api():
    calls = re.findall(r"\b(?:fetch|EventSource)\(\s*([^)]*)", SCRIPT)
    assert calls
    for arg in calls:
        if arg.startswith("path,"):  # the one wrapper: fetch(path, init); its callers are checked below
            continue
        assert re.match(r"""['"]/api/""", arg), arg
    assert "request('/api/' + path" in SCRIPT
    callers = re.findall(r"(?<!function )\b(?:getJSON|postJSON)\(\s*([^,)]*)", SCRIPT)
    assert callers and all(re.match(r"""['"][a-z]""", c) for c in callers)


def test_api_strings_go_in_by_text_nodes_only():
    assert "textContent" in SCRIPT and "createTextNode" in SCRIPT
    for call in re.findall(r"setAttribute\(([^)]*)\)", SCRIPT):
        assert re.fullmatch(r"""\s*'[^']*'\s*,\s*'[^']*'\s*""", call), call
    assert not re.search(r"\.(?:href|src|srcdoc|action)\s*=", SCRIPT)


def test_inline_script_parses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "inline.js"
        path.write_text(SCRIPT, encoding="utf-8")
        done = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_required_text_is_present():
    assert (
        "Audience data from Qloo. The plans on this page were pre-computed and are stored privately. "
        "Affinity describes aggregate audiences, not individuals or what any person will do."
    ) in PAGE
    assert "not included in this repository" not in PAGE and "hackathon organizers" not in PAGE
    for words in (
        "identified as a comedian", "not identified as a comedian", "not sponsorship intent",
        "Waking the server, this can take about a minute", "Qloo result", "Roadie reading", "Copy pitch",
        "navigator.clipboard.writeText", "prefers-reduced-motion", ":focus-visible",
    ):
        assert words in PAGE, words


def test_live_status_says_how_long_a_run_takes_and_that_steps_appear_as_they_finish():
    assert "A live run takes about one to two minutes, and steps appear as they finish." in SCRIPT
    start = SCRIPT.index("function liveStatusLine")
    body = SCRIPT[start : SCRIPT.index("function start(", start)]
    assert "textContent" in body and "innerHTML" not in body  # still plain text only


def test_colors_and_fonts_are_variables():
    root = re.search(r":root\s*\{(.*?)\}", PAGE, re.S).group(1)
    assert "--font-display" in root and "Impact" in root and "--accent" in root and "--bg" in root
    css = re.search(r"<style>(.*?)</style>", PAGE, re.S).group(1)
    rest = css.split("}", 1)[1]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", rest)


def test_every_live_error_code_has_a_fixed_sentence():
    for code in ("live_disabled", "live_ended", "daily_limit", "monthly_limit", "ip_limit", "too_many_jobs", "qloo_unavailable"):
        assert f"{code}:" in SCRIPT


def test_csp_constant_matches_the_spec():
    assert CSP == EXPECTED_CSP


def test_root_serves_the_page_with_csp_and_security_headers(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path)))
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["content-security-policy"] == EXPECTED_CSP
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert r.text == PAGE
    assert client.get("/api/health").status_code == 200


def test_synthetic_gallery_has_the_shapes_the_page_reads(tmp_path):
    bg.build_gallery(SYNTH_DIR, tmp_path / "gallery", narrator=TemplateNarrator())
    client = TestClient(create_app(Settings(data_dir=tmp_path)))
    rows = client.get("/api/gallery").json()
    assert sorted(r["slug"] for r in rows) == SLUGS
    assert all({"name", "description", "built_with", "strong_city_count", "identified_comics"} <= set(r) for r in rows)
    entry = client.get(f"/api/plan/{JUNE}").json()
    for key in ("cities", "route_suggestion", "comics_to_bill", "comedy_venues", "sponsor_candidates", "city_tier_note"):
        assert key in entry["plan"]
    assert entry["narration"]["city_pitches"] and entry["narration"]["booking_pitch"]


# ---- milestone 13: polish and copy (rendered with node against a tiny fake DOM; skipped without node) ----------

HARNESS = ROOT / "backend" / "tests" / "frontend_harness.js"
OLD_NOTE = "Roadie marks a person as identified_as_comedian when their one-line Qloo description contains the word comic."


def run_harness(tmp_path, payload):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    data = tmp_path / "input.json"
    data.write_text(json.dumps(payload), encoding="utf-8")
    done = subprocess.run([node, str(HARNESS), str(ROOT / "frontend" / "index.html"), str(data)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def walk(node):
    yield node
    for kid in node.get("k", []):
        yield from walk(kid)


def text_of(node):
    return node["x"] if "x" in node else "".join(text_of(k) for k in node.get("k", []))


def cls(node):
    return set(node.get("c", "").split())


def find(root, tag=None, klass=None):
    return [n for n in walk(root) if "x" not in n and (tag is None or n["t"] == tag) and (klass is None or klass in cls(n))]


def section_named(root, title):
    for sec in find(root, "section", "block"):
        heads = find(sec, "h2")
        if heads and text_of(heads[0]).startswith(title):
            return sec
    raise AssertionError(title)


@pytest.fixture(scope="module")
def entries(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gallery")
    bg.build_gallery(SYNTH_DIR, tmp / "gallery", narrator=TemplateNarrator())
    client = TestClient(create_app(Settings(data_dir=tmp)))
    return {slug: client.get(f"/api/plan/{slug}").json() for slug in SLUGS}


def test_old_wording_is_gone_and_internal_field_names_are_not_shown(tmp_path, entries):
    assert "identified_as_comedian" not in " ".join(re.findall(r"'[^'\n]*'", SCRIPT.replace("c.identified_as_comedian", "")))
    ent = json.loads(json.dumps(entries[JUNE]))
    ent["plan"]["comics_to_bill"]["note"] = OLD_NOTE  # a plan stored before this change
    out = run_harness(tmp_path, {"entries": [ent, entries[HARLAN]], "copy": [OLD_NOTE]})
    assert out["cleaned"] == ["Roadie marks a person as an identified comedian when their one-line Qloo description contains the word comic."]
    for tree in out["plans"]:
        for n in walk(tree):
            assert "identified_as_comedian" not in n.get("x", "")
    assert "an identified comedian" in text_of(section_named(out["plans"][0], "Comics to bill with"))


def test_backend_explainer_uses_the_plain_phrase(entries):
    for slug in SLUGS:
        note = entries[slug]["plan"]["comics_to_bill"]["note"]
        note = note if isinstance(note, str) else note["text"]
        assert "identified_as_comedian" not in note and "an identified comedian" in note


def test_gallery_card_with_no_identified_comics(tmp_path):
    base = {"slug": "x", "name": "Invented Comic", "description": "d", "built_with": "template", "strong_city_count": 2}
    out = run_harness(tmp_path, {"galleryItems": [{**base, "identified_comics": 0}, {**base, "identified_comics": 1}, {**base, "identified_comics": 3}]})
    zero, one, three = out["galleryText"]
    assert "No comics identified" in zero and "0 identified comics" not in zero
    assert "1 identified comic" in one and "3 identified comics" in three


def test_repeated_tier_is_one_pure_function(tmp_path):
    out = run_harness(tmp_path, {
        "tierLists": [["strong"] * 4, ["strong", "strong", "strong", "moderate", "weaker"], ["strong"], []],
        "badgeChecks": [[["strong"] * 4, "strong"], [["strong", "strong", "moderate"], "strong"], [["strong", "strong", "moderate"], "moderate"]],
    })
    same, mixed, single, empty = out["tierSummary"]
    assert same["uniform"] and same["text"] == "All 4 cities: strong affinity"
    assert not mixed["uniform"] and mixed["baseline"] == "strong" and mixed["text"] == "3 of 5 cities: strong affinity"
    assert single["text"] == "The one city: strong affinity" and empty["text"] == ""
    assert out["needsBadge"] == [False, False, True]


def test_route_strip_has_one_tier_line_no_arrows_and_a_caption(tmp_path, entries):
    assert "\\2192" not in PAGE and not re.search("[\u2190-\u21ff\u2794\u27a1]", PAGE) and "->" not in SCRIPT.replace("=>", "")
    out = run_harness(tmp_path, {"entries": [entries[JUNE]]})
    route = section_named(out["plans"][0], "Suggested route")
    strip = find(route, "ol", "route")[0]
    strip_text = text_of(strip)
    assert not re.search("[\u2190-\u21ff\u2794\u27a1]|->", strip_text)
    cards = find(strip, "button", "stop")
    assert len(cards) >= 2
    for card in cards:  # same tier everywhere: no badge on a card, no rank numbers
        assert not find(card, "span", "badge") and not re.search(r"\d", text_of(card))
    texts = [text_of(n) for n in find(route, "p")]
    assert any(re.fullmatch(r"All \d+ cities: strong affinity", t) for t in texts)
    assert "West to east" in texts
    assert [n for n in walk(route)].index(find(route, "p", "route-caption")[0]) < [n for n in walk(route)].index(strip)
    for d in find(route, "details")[:-1]:  # the last one is "All scored cities"
        assert not find(find(d, "summary")[0], "span", "badge")


def test_city_rows_have_chevron_and_preview_and_route_cards_open_them(tmp_path, entries):
    out = run_harness(tmp_path, {"entries": [entries[JUNE]]})
    route = section_named(out["plans"][0], "Suggested route")
    rows = find(route, "details")[:-1]
    assert rows
    previews = []
    for d in rows:
        summary = find(d, "summary")[0]
        assert find(summary, "span", "chev")
        previews.append(text_of(find(summary, "span", "preview")[0]))
    assert all(re.match(r"(\d+ venues?|no venues listed)(\. Limited data:.*)?$", p) for p in previews), previews
    assert any(p.startswith("no venues listed") for p in previews)
    click = out["clicks"][0]
    assert click["buttons"] == len(rows) and not any(click["before"])
    assert click["after"][len(rows) - 1] and click["scrolled"][len(rows) - 1]
    assert not click["after"][0] or len(rows) == 1


def test_labels_one_qloo_label_per_list_and_relation_pills_are_roadie(tmp_path, entries):
    out = run_harness(tmp_path, {"entries": [entries[JUNE]]})
    root = out["plans"][0]
    route = section_named(root, "Suggested route")
    for d in find(route, "details")[:-1]:
        for ul in find(d, "ul", "plain"):
            assert not find(ul, "span", "badge")
        for h4 in find(d, "h4"):
            if text_of(h4).startswith("Comedy venues") and find(d, "ul", "plain"):
                assert len(find(h4, "span", "qloo")) == 1
    comics = section_named(root, "Comics to bill with")
    people = find(comics, "ul", "people")[0]
    for li in find(people, "li"):
        top = find(li, "p")[0]
        labels = {text_of(b): cls(b) for b in find(top, "span", "badge")}
        assert "qloo" in labels["Qloo result"]
        for word in ("peer", "bigger act", "smaller act"):
            if word in labels:
                assert "roadie" in labels[word] and "qloo" not in labels[word]
        assert not find(top, "span", "tag")
    assert any(text_of(b) in ("peer", "bigger act", "smaller act") for b in find(people, "span", "badge"))
    sponsors = section_named(root, "Sponsor candidates")
    assert len(find(find(sponsors, "h2")[0], "span", "qloo")) == 1
    assert not find(find(sponsors, "ul", "chips")[0], "span", "badge")


def test_comics_section_with_no_identified_comics_is_a_sentence_not_an_empty_list(tmp_path, entries):
    empty = json.loads(json.dumps(entries[HARLAN]))
    empty["plan"]["comics_to_bill"]["items"] = []
    out = run_harness(tmp_path, {"entries": [entries[HARLAN], empty]})
    for tree in out["plans"]:
        comics = section_named(tree, "Comics to bill with")
        assert "Roadie suggests no comic to bill with" in text_of(comics) or "Qloo returned no people" in text_of(comics)
    harlan = section_named(out["plans"][0], "Comics to bill with")
    assert "No comedians were identified" in text_of(harlan)
    bare = section_named(out["plans"][1], "Comics to bill with")
    assert not find(bare, "ul") and "Qloo returned no people with overlapping audiences." in text_of(bare)


def test_css_gallery_footer_row_is_pinned_to_the_bottom_and_chevron_is_drawn():
    assert re.search(r"\.card \.foot\s*\{[^}]*margin-top:\s*auto", PAGE)
    assert ".chev" in PAGE and "West to east" in PAGE


# ---- milestone 14: size labels only on identified comedians, venue-only previews, narrow layout -------------

def comics_plan(entry, items):
    plan = {**json.loads(json.dumps(entry["plan"])), "comics_to_bill": {"items": items}}
    return {**entry, "plan": plan}


def person(name, identified, relation="smaller act", description="Invented one-liner."):
    return {"name": name, "relation": relation, "identified_as_comedian": identified, "description": description}


SIZE_WORDS = ("peer", "bigger act", "smaller act")
NOT_IDENTIFIED_HEAD = "People Qloo returned who are not identified as comedians"


def badge_words(node):
    return [text_of(b) for b in find(node, "span", "badge")]


def test_size_label_only_on_identified_comedians_and_the_rest_are_collapsed(tmp_path, entries):
    mixed = comics_plan(entries[JUNE], [
        person("Zed Unidentified", False, "smaller act", "Invented chef and author."),
        person("Ana Identified", True, "peer", "Invented comedian."),
        person("Bo Unknown Kind", False, "bigger act", "Invented podcaster."),
        person("Cy Identified", True, "bigger act", "Invented comic."),
    ])
    out = run_harness(tmp_path, {"entries": [mixed]})
    comics = section_named(out["plans"][0], "Comics to bill with")
    main = find(comics, "ul", "people")
    assert len(main) == 2
    names = [text_of(find(li, "span", "who")[0]) for li in find(main[0], "li")]
    assert names == ["Ana Identified", "Cy Identified"]
    assert [w for w in badge_words(main[0]) if w in SIZE_WORDS] == ["peer", "bigger act"]
    details = find(comics, "details")
    assert len(details) == 1 and text_of(find(details[0], "summary")[0]) == NOT_IDENTIFIED_HEAD
    assert [n for n in walk(comics) if n is details[0]] and list(walk(comics)).index(details[0]) > list(walk(comics)).index(main[0])
    rest = find(details[0], "li")
    assert [text_of(find(li, "span", "who")[0]) for li in rest] == ["Zed Unidentified", "Bo Unknown Kind"]
    for li in rest:
        words = badge_words(li)
        assert "not identified as a comedian" in words and not set(words) & set(SIZE_WORDS)
    assert "Invented chef and author." in text_of(details[0])
    assert "No comedians were identified" not in text_of(comics)


def test_no_identified_people_puts_the_sentence_directly_above_the_collapsed_list(tmp_path, entries):
    none = comics_plan(entries[JUNE], [person("Dee Chef", False, "peer", "Invented chef."), person("Eli Author", False, "smaller act", "Invented author.")])
    out = run_harness(tmp_path, {"entries": [none]})
    comics = section_named(out["plans"][0], "Comics to bill with")
    kids = [k for k in comics["k"] if "x" not in k]
    assert not [k for k in kids if k["t"] == "ul"] and len(find(comics, "details")) == 1
    sentence = [i for i, k in enumerate(kids) if "No comedians were identified" in text_of(k)]
    assert sentence and kids[sentence[0] + 1]["t"] == "details"
    assert not set(badge_words(comics)) & set(SIZE_WORDS)


def test_null_identification_keeps_the_not_run_wording_and_has_no_size_label(tmp_path, entries):
    live = comics_plan(entries[JUNE], [person("Fay Live", None, "peer", ""), person("Gus Live", None, "bigger act", "")])
    out = run_harness(tmp_path, {"entries": [{**live, "mode": "live"}]})
    comics = section_named(out["plans"][0], "Comics to bill with")
    words = badge_words(comics)
    assert words.count("identification not run for live plans") == 2
    assert not set(words) & set(SIZE_WORDS) and not find(comics, "details")
    assert "Live plans do not identify comics as comedians" in text_of(comics)


def test_city_preview_is_only_the_venue_count_and_the_limited_data_note(tmp_path, entries):
    out = run_harness(tmp_path, {"entries": [entries[JUNE]]})
    route = section_named(out["plans"][0], "Suggested route")
    previews = [text_of(find(find(d, "summary")[0], "span", "preview")[0]) for d in find(route, "details")[:-1]]
    assert previews and not any("comic" in p for p in previews), previews
    assert any(p == "no venues listed" for p in previews) or any(p.startswith("no venues listed") for p in previews)
    assert all(re.match(r"(\d+ venues?|no venues listed)(\. Limited data:.*)?$", p) for p in previews), previews
    assert any(". Limited data:" in p for p in previews)


# ---- narrow layout, headless Chromium (skipped when node, Chromium or playwright-core is missing) -------------

BROWSER_SCRIPT = ROOT / "backend" / "tests" / "browser_overflow.js"
LONG_VENUE = "The Extraordinarily-Long-Named-Invented-Comedy-Cellar-And-Supper-Club-Of-Nowhere-Springs"
LONG_BRAND = "InventedSuperLongBrandNameThatKeepsGoingWithoutAnySpacesAtAllToTestWrapping"


def find_chromium():
    for cand in (shutil.which("chromium"), shutil.which("chromium-browser"), shutil.which("google-chrome"), "/opt/pw-browsers/chromium"):
        if cand and Path(cand).exists():
            return cand
    return None


@pytest.fixture(scope="module")
def narrow_report(tmp_path_factory):
    node, chrome = shutil.which("node"), find_chromium()
    if node is None or chrome is None:
        pytest.skip("node or headless Chromium is not available")
    tmp = tmp_path_factory.mktemp("narrow")
    bg.build_gallery(SYNTH_DIR, tmp / "gallery", narrator=TemplateNarrator())
    client = TestClient(create_app(Settings(data_dir=tmp)))
    routes = {"/": {"type": "text/html; charset=utf-8", "body": client.get("/").text, "headers": {"content-security-policy": EXPECTED_CSP}}}
    routes["/api/health"] = {"type": "application/json", "body": json.dumps(client.get("/api/health").json())}
    rows = client.get("/api/gallery").json()
    routes["/api/gallery"] = {"type": "application/json", "body": json.dumps(rows)}
    for slug in SLUGS:
        entry = client.get(f"/api/plan/{slug}").json()
        plan = entry["plan"]
        for city in plan["comedy_venues"]["cities"]:
            if city.get("items"):
                city["items"][0]["name"] = LONG_VENUE
        for brand in plan["sponsor_candidates"]["items"][:1]:
            brand["name"] = LONG_BRAND
        routes[f"/api/plan/{slug}"] = {"type": "application/json", "body": json.dumps(entry)}
    data = tmp / "in.json"
    data.write_text(json.dumps({"chrome": chrome, "widths": [360, 390], "routes": routes}), encoding="utf-8")
    try:
        done = subprocess.run([node, str(BROWSER_SCRIPT), str(data)], capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        pytest.skip("headless Chromium timed out")
    if done.returncode != 0:
        pytest.skip(f"headless Chromium could not run: {done.stderr[-200:]}")
    report = json.loads(done.stdout)
    if isinstance(report, dict) and report.get("skip"):
        pytest.skip(report["skip"])
    return report


def test_no_sideways_scroll_at_360_and_390(narrow_report):
    assert [r["width"] for r in narrow_report] == [360, 390]
    for row in narrow_report:
        assert row["gallery"]["scrollWidth"] <= row["gallery"]["innerWidth"], row["gallery"]
        assert row["plans"]
        for name, m in row["plans"].items():
            assert m["scrollWidth"] <= m["innerWidth"], (row["width"], name, m)


def test_buttons_and_city_rows_are_at_least_44px_tall_on_narrow_screens(narrow_report):
    for row in narrow_report:
        assert min(row["homeButtons"]) >= 44
        for name, m in row["plans"].items():
            assert min(m["small"]["buttons"]) >= 44 and min(m["small"]["cityRows"]) >= 44, (row["width"], name, m["small"])


# ---- milestone 18: the page and docs tell the truth about live mode on the hosted demo -----------------------

CALM = ("Live search is switched off on this demo server because the free host does not have enough CPU for it. "
        "The pre-built plans above are the demo.")


def test_calm_sentence_is_in_the_page_and_uses_the_notice_style_not_the_problem_label():
    start = SCRIPT.index("function liveStatusLine")
    body = SCRIPT[start : SCRIPT.index("function start(", start)]
    assert CALM in body
    assert "problem(" not in body and "'problem'" not in body


def test_live_disabled_hides_the_form_and_shows_the_calm_notice(tmp_path):
    out = run_harness(tmp_path, {"healths": [
        {"status": "ok", "live_enabled": False, "live_budget_remaining": None, "gallery_count": 5},
        {"status": "ok", "live_enabled": True, "live_budget_remaining": 7, "gallery_count": 5},
    ]})
    off, on = out["live"]
    assert off["formHidden"] is True and off["ledeText"] == CALM and off["ledeClass"] == "notice"
    assert off["status"] == ""
    assert on["formHidden"] is False and on["ledeText"] is None and on["ledeClass"] is None
    assert "Live runs left in the current budget: 7." in on["status"]


def test_docs_state_the_known_limits():
    for rel in ("docs/DEPLOY.md", "docs/API.md", "docs/ARCHITECTURE.md", "CLAUDE.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "Known limits" in text, rel
        for fact in ("133", "47 seconds" if rel.startswith("docs") else "47 s", "ROADIE_LIVE=0", "Nov 16"):
            assert fact in text, (rel, fact)
