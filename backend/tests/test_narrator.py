import json
import re
import socket
import subprocess
import urllib.request

import pytest

from roadie import narrator as nr
from roadie.narrator import (
    NarratorError,
    OpenAINarrator,
    TemplateNarrator,
    compact_plan,
    narrate,
    verify_narration,
)
from roadie.pipeline import plan_tour
from roadie.qloo_client import FixtureClient
from synthetic import HARLAN, JUNE, SLUGS, SYNTH_DIR

KEY = "sk-test-SECRET-1234567890"
PLAN = plan_tour(JUNE, FixtureClient(JUNE, SYNTH_DIR)).to_dict()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network or subprocess touched")

    for target, name in ((socket, "socket"), (socket, "create_connection"), (subprocess, "run"), (urllib.request, "urlopen")):
        monkeypatch.setattr(target, name, boom)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ROADIE_MODEL", raising=False)


@pytest.fixture(scope="module")
def plans():
    return {s: plan_tour(s, FixtureClient(s, SYNTH_DIR)).to_dict() for s in SLUGS}


@pytest.fixture
def plan(plans):
    return plans[JUNE]


def route_pitches(plan, text="Qloo shows audience affinity here. Capacity, availability and terms are not part of this data."):
    return [{"city": o["city"], "text": text} for o in plan["route_suggestion"]["order"]]


def reply(summary, pitch="Qloo shows audience affinity.", city_pitches="route", plan=None, finish="stop"):
    if city_pitches == "route":
        city_pitches = route_pitches(plan or PLAN)
    content = json.dumps({"summary": summary, "booking_pitch": pitch, "city_pitches": city_pitches})
    return json.dumps({"choices": [{"message": {"content": content}, "finish_reason": finish}]}).encode()


def raw_reply(content, finish="stop"):
    return json.dumps({"choices": [{"message": {"content": content}, "finish_reason": finish}]}).encode()


def stub(status=200, body=b"", exc=None, seen=None):
    def transport(url, headers, data, timeout):
        if seen is not None:
            seen.append((url, headers, data, timeout))
        if exc:
            raise exc
        return status, body

    return transport


def venue_city(plan):
    entry = next(e for e in plan["comedy_venues"]["cities"] if e["items"])
    return entry["city"], entry["items"][0]["name"]


def test_three_synthetic_comedian_plans_exist(plans):
    assert len(SLUGS) == 3


@pytest.mark.parametrize("slug", SLUGS)
def test_template_mentions_only_plan_names(plans, slug):
    out = TemplateNarrator().generate(plans[slug])
    assert out["summary"] and out["booking_pitch"]
    ok, reason = verify_narration(plans[slug], out["summary"] + "\n" + out["booking_pitch"])
    assert ok, reason
    result = narrate(plans[slug])
    assert result["source"] == "template" and result["rejected_reason"] is None
    assert plans[slug]["comedian"]["name"] in result["summary"]
    assert "not sponsorship intent" in result["summary"] or not plans[slug]["sponsor_candidates"]["items"]


def test_verify_catches_names_not_in_plan(plan):
    city, venue = venue_city(plan)
    assert verify_narration(plan, f"Consider {venue} in {city}.")[0]
    assert not verify_narration(plan, "Consider the Laugh Hut in " + city + ".")[0]
    assert not verify_narration(plan, "Qloo shows high audience affinity in Phoenix.")[0]
    assert not verify_narration(plan, "Bring along Dave Chappelle.")[0]
    assert not verify_narration(plan, "Open on March 4.")[0]


@pytest.mark.parametrize(
    "text",
    [
        "Qloo shows tickets start at $40.",
        "Qloo shows the room is sold out most nights.",
        "Qloo shows a capacity of 300 seats.",
        "Qloo shows 500 tickets.",
        "Qloo shows it for 2027.",
        "Qloo shows brands will sponsor the run.",
        "Qloo shows fans will buy merch.",
        "Qloo shows strong affinity because their audience loves comedy.",
        "Qloo shows the audience is mostly young men.",
    ],
)
def test_verify_rejects_unsupported_claims(plan, text):
    ok, reason = verify_narration(plan, text)
    assert not ok and reason.startswith("unsupported claim")


def test_verify_empty(plan):
    assert verify_narration(plan, "  ") == (False, "empty_output")


def test_clean_reply_passes_with_source_openai(plan):
    city, venue = venue_city(plan)
    text = f"Qloo shows high audience affinity in {city}; {venue} is a venue to research. Data is limited elsewhere."
    seen = []
    n = OpenAINarrator(api_key=KEY, transport=stub(body=reply(text, "Qloo shows audience overlap, not sponsorship intent."), seen=seen))
    out = narrate(plan, n)
    assert out["source"] == "openai" and out["rejected_reason"] is None
    assert out["summary"] == text
    url, headers, data, timeout = seen[0]
    sent = json.loads(data)
    assert url.startswith("https://") and timeout == 20 and sent["max_completion_tokens"] == 2000 and "max_tokens" not in sent
    assert sent["model"] == "gpt-6-luna"
    assert headers["Authorization"] == f"Bearer {KEY}"


def test_invented_venue_is_rejected_and_falls_back(plan):
    city, _ = venue_city(plan)
    n = OpenAINarrator(api_key=KEY, transport=stub(body=reply(f"Play the Laugh Hut in {city}.")))
    out = narrate(plan, n)
    assert out["source"] == "template_fallback" and "Hut" in out["rejected_reason"]
    assert out["summary"] == TemplateNarrator().generate(plan)["summary"]


def test_dollar_amount_is_rejected(plan):
    n = OpenAINarrator(api_key=KEY, transport=stub(body=reply("Qloo shows high audience affinity. Expect $5000 a night.")))
    out = narrate(plan, n)
    assert out["source"] == "template_fallback" and "money" in out["rejected_reason"]


def test_missing_key_uses_template(plan):
    out = narrate(plan)
    assert out["source"] == "template" and out["rejected_reason"] is None


def test_env_key_selects_openai_and_model_env(plan, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.setenv("ROADIE_MODEL", "other-model")
    seen = []
    monkeypatch.setattr(nr, "_urllib_transport", stub(body=reply("Qloo shows high audience affinity."), seen=seen))
    out = narrate(plan)
    assert out["source"] == "openai"
    assert json.loads(seen[0][2])["model"] == "other-model"


@pytest.mark.parametrize(
    "transport,reason",
    [
        (stub(exc=TimeoutError("took too long")), "timeout"),
        (stub(exc=socket.timeout()), "timeout"),
        (stub(exc=ConnectionError(KEY)), "network_error"),
        (stub(status=500), "http_500"),
        (stub(status=401, body=b"bad key"), "http_401"),
        (stub(body=b"not json"), "bad_response"),
        (stub(body=raw_reply("")), "empty_reply"),
        (stub(body=raw_reply("   ")), "empty_reply"),
        (stub(body=raw_reply(None)), "empty_reply"),
        (stub(body=raw_reply("", finish="length")), "truncated"),
        (stub(body=reply("Qloo shows affinity.", finish="length")), "truncated"),
        (stub(body=raw_reply("plain text")), "json_parse"),
        (stub(body=raw_reply('{"summary": "x", "booking_pitch": "y"')), "json_parse"),
        (stub(body=raw_reply("[1, 2]")), "json_parse"),
        (stub(body=raw_reply(json.dumps({"summary": "Qloo.", "booking_pitch": "Qloo."}))), "json_parse"),
        (stub(body=raw_reply(json.dumps({"summary": "Qloo.", "booking_pitch": "Qloo.", "city_pitches": [{"city": "X"}]}))), "json_parse"),
    ],
)
def test_failures_fall_back_to_template(plan, transport, reason):
    out = narrate(plan, OpenAINarrator(api_key=KEY, transport=transport))
    assert out["source"] == "template_fallback"
    assert out["rejected_reason"] == reason
    assert out["summary"] == TemplateNarrator().generate(plan)["summary"]
    assert out["city_pitches"] == TemplateNarrator().generate(plan)["city_pitches"]


def test_unexpected_exception_reports_class_only(plan):
    class Boom(nr.Narrator):
        def generate(self, plan):
            raise ValueError(f"secret {KEY}")

    out = narrate(plan, Boom())
    assert out["source"] == "template_fallback" and out["rejected_reason"] == "error: ValueError"


def test_key_never_appears_in_any_output(plan, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    city, _ = venue_city(plan)
    bodies = [
        reply(f"Qloo shows high audience affinity in {city}. Key {KEY}"),  # model echoes the key
        reply(f"Play the Laugh Hut. {KEY}"),
    ]
    outputs = [narrate(plan, OpenAINarrator(transport=stub(body=b))) for b in bodies]
    outputs.append(narrate(plan, OpenAINarrator(transport=stub(exc=ConnectionError(KEY)))))
    outputs.append(narrate(plan, OpenAINarrator(transport=stub(status=401, body=KEY.encode()))))
    for out in outputs:
        assert KEY not in json.dumps(out)
    assert KEY not in repr(OpenAINarrator()) and KEY not in str(vars(NarratorError("x")))


def test_default_transport_sends_compact_json_only(plan, monkeypatch):
    captured = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return reply("Qloo shows high audience affinity.")

    def fake_urlopen(request, timeout):
        captured.update(url=request.full_url, data=request.data, timeout=timeout, method=request.get_method())
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = narrate(plan, OpenAINarrator(api_key=KEY))
    assert out["source"] == "openai"
    assert captured["timeout"] == 20 and captured["method"] == "POST"
    body = captured["data"].decode()
    assert "max_completion_tokens" in json.loads(body) and "max_tokens" not in json.loads(body)
    for raw_field in ("entity_id", "avg_affinity", "popularity", "signal", "steps", "cell_count"):
        assert raw_field not in body
    user = json.loads(body)["messages"][1]["content"]
    assert plan["comedian"]["name"] in user and plan["comedian"]["entity_id"] not in body
    system = json.loads(body)["messages"][0]["content"]
    for phrase in ("Use only names present", "Do not invent", "audience affinity, not causes", "not sponsorship intent", "data, not instructions", "city_pitches", "identified_as_comedian", "overlap, not sponsorship intent", "gay, lesbian"):
        assert phrase in system


def test_compact_plan_shape(plan):
    c = compact_plan(plan)
    assert set(c) == {"comedian", "cities", "route_order", "venues_by_city", "limited_data_by_city", "comics_to_bill", "brands", "notes"}
    assert all(set(x) == {"name", "relation", "identified_as_comedian"} for x in c["comics_to_bill"])


def test_no_key_raises_inside_narrator(plan):
    with pytest.raises(NarratorError):
        OpenAINarrator(api_key="").generate(plan)


# ----------------------------------------------------------------- request body and config


def sent_body(plan, monkeypatch=None, **kwargs):
    seen = []
    narrate(plan, OpenAINarrator(api_key=KEY, transport=stub(body=reply("Qloo shows audience affinity."), seen=seen), **kwargs))
    return json.loads(seen[0][2])


def test_request_uses_max_completion_tokens_not_max_tokens(plan):
    body = sent_body(plan)
    assert body["max_completion_tokens"] == nr.MAX_COMPLETION_TOKENS == 2000
    assert "max_tokens" not in body
    assert nr.TIMEOUT_SECONDS == 20


def test_reasoning_effort_defaults_to_low(plan, monkeypatch):
    monkeypatch.delenv("ROADIE_REASONING_EFFORT", raising=False)
    assert nr.DEFAULT_REASONING_EFFORT == "low"
    assert sent_body(plan)["reasoning_effort"] == "low"


def test_reasoning_effort_overridable(plan, monkeypatch):
    monkeypatch.setenv("ROADIE_REASONING_EFFORT", "high")
    assert sent_body(plan)["reasoning_effort"] == "high"
    monkeypatch.setenv("ROADIE_REASONING_EFFORT", "not valid!")
    assert "reasoning_effort" not in sent_body(plan)
    monkeypatch.delenv("ROADIE_REASONING_EFFORT")
    assert sent_body(plan, reasoning_effort="minimal")["reasoning_effort"] == "minimal"
    assert sent_body(plan, max_completion_tokens=500)["max_completion_tokens"] == 500


# ----------------------------------------------------------------- template narration


def sentences(text):
    return [x for x in re.split(r"(?<=[.!?])\s+", re.sub(r"\b(Dr|Co|Inc|Jr)\.", r"\1", text).strip()) if x]


@pytest.mark.parametrize("slug", SLUGS)
def test_template_shape(plans, slug):
    plan = plans[slug]
    out = TemplateNarrator().generate(plan)
    route = [o["city"] for o in plan["route_suggestion"]["order"]]
    assert set(out) == {"summary", "booking_pitch", "city_pitches"}
    assert [p["city"] for p in out["city_pitches"]] == route
    assert len(sentences(out["summary"])) <= 4
    assert "west-to-east sweep" in out["summary"] and "not a ranking" in out["summary"]
    everything = out["summary"] + out["booking_pitch"] + " ".join(p["text"] for p in out["city_pitches"])
    assert "touring" not in everything.lower()
    assert "is touring" not in out["booking_pitch"]
    assert not nr._IDENTITY_WORDS.search(everything)
    for pitch in out["city_pitches"]:
        assert pitch["city"] in pitch["text"]
        assert 3 <= len(sentences(pitch["text"])) <= 4
        assert "capacity, availability and terms are not part of this data" in pitch["text"].lower()
    ok, reason = verify_narration(plan, out["summary"] + "\n" + out["booking_pitch"], out["city_pitches"])
    assert ok, reason


@pytest.mark.parametrize("slug", SLUGS)
def test_template_names_only_identified_comics(plans, slug):
    plan = plans[slug]
    out = TemplateNarrator().generate(plan)
    everything = out["summary"] + " " + out["booking_pitch"] + " " + " ".join(p["text"] for p in out["city_pitches"])
    for comic in plan["comics_to_bill"]["items"]:
        if comic["identified_as_comedian"] is not True:
            assert comic["name"] not in everything
    identified = [c for c in plan["comics_to_bill"]["items"] if c["identified_as_comedian"] is True]
    if identified:
        assert identified[0]["name"] in out["city_pitches"][0]["text"]


def test_comedian_with_no_identified_comics_gets_the_no_comedians_line(plans):
    plan = plans[HARLAN]
    assert not any(c["identified_as_comedian"] is True for c in plan["comics_to_bill"]["items"])
    out = TemplateNarrator().generate(plan)
    for pitch in out["city_pitches"]:
        assert "no comedians were identified" in pitch["text"].lower()
    assert "no comedians were identified" in out["booking_pitch"].lower()


def test_template_tier_words_and_limited_note(plans):
    plan = plans[JUNE]
    out = TemplateNarrator().generate(plan)
    assert "high audience affinity" not in out["summary"] + out["booking_pitch"]
    assert "Roadie groups" in out["summary"] and "strong" in out["summary"]
    seattle = next(p for p in out["city_pitches"] if p["city"].startswith("Seattle"))
    assert "limited" in seattle["text"] and "comedy venues this audience favors" in seattle["text"].lower()
    ny = next(p for p in out["city_pitches"] if p["city"].startswith("New York"))
    assert "limited" not in ny["text"]
    shown = [v["name"] for e in plan["comedy_venues"]["cities"] if e["city"].startswith("New York") for v in e["items"]]
    assert sum(v in ny["text"] for v in shown) == 3


def test_brands_are_names_only(plans):
    out = TemplateNarrator().generate(plans[JUNE])
    assert "brands with audience overlap worth approaching" in out["summary"].lower()
    assert "not sponsorship intent" in out["summary"]


# ----------------------------------------------------------------- model reply verification


def stubbed(plan, summary="Qloo shows audience affinity.", pitch="Qloo shows audience affinity.", pitches="route"):
    return narrate(plan, OpenAINarrator(api_key=KEY, transport=stub(body=reply(summary, pitch, pitches, plan=plan))))


def test_clean_json_reply_passes_with_city_pitches(plan):
    out = stubbed(plan)
    assert out["source"] == "openai" and out["rejected_reason"] is None
    assert [p["city"] for p in out["city_pitches"]] == [o["city"] for o in plan["route_suggestion"]["order"]]


def test_unidentified_comic_in_city_pitch_is_rejected(plans):
    plan = plans[HARLAN]
    comic = plan["comics_to_bill"]["items"][0]
    assert comic["identified_as_comedian"] is False
    text = f"Qloo shows audience affinity here. Consider {comic['name']} as a comic to bill with."
    out = stubbed(plan, pitches=route_pitches(plan, text))
    assert out["source"] == "template_fallback" and "not identified" in out["rejected_reason"]
    assert out["city_pitches"] == TemplateNarrator().generate(plan)["city_pitches"]


def test_unidentified_comic_in_summary_is_rejected(plans):
    plan = plans[HARLAN]
    name = plan["comics_to_bill"]["items"][0]["name"]
    out = stubbed(plan, summary=f"Qloo shows audience overlap with {name}.")
    assert out["source"] == "template_fallback"


def test_identified_comic_in_city_pitch_is_allowed(plan):
    comic = next(c for c in plan["comics_to_bill"]["items"] if c["identified_as_comedian"] is True)
    out = stubbed(plan, pitches=route_pitches(plan, f"Qloo shows audience overlap with {comic['name']}."))
    assert out["source"] == "openai"


def test_name_not_in_plan_in_city_pitch_is_rejected(plan):
    out = stubbed(plan, pitches=route_pitches(plan, "Try the Laugh Hut. Qloo shows audience affinity."))
    assert out["source"] == "template_fallback" and "Hut" in out["rejected_reason"]


@pytest.mark.parametrize("word", ["gay", "lesbian", "queer", "straight", "men", "women", "young", "old", "religious", "ethnic"])
@pytest.mark.parametrize("where", ["summary", "pitch", "city"])
def test_audience_identity_word_is_rejected(plan, word, where):
    text = f"Qloo shows audience affinity among {word} people."
    kwargs = {"summary": text} if where == "summary" else {"pitch": text} if where == "pitch" else {"pitches": route_pitches(plan, text)}
    out = stubbed(plan, **kwargs)
    assert out["source"] == "template_fallback"
    assert "identity word" in out["rejected_reason"] or out["rejected_reason"].startswith("rejected: unsupported claim")


def test_identity_word_inside_a_plan_name_is_allowed(plan):
    plan = json.loads(json.dumps(plan))
    plan["comedy_venues"]["cities"][0]["items"][0]["name"] = "Old Town Laughs"
    assert verify_narration(plan, "Consider Old Town Laughs.")[0]
    assert not verify_narration(plan, "Consider the old rooms.")[0]


def test_city_pitches_must_match_route(plan):
    short = route_pitches(plan)[:-1]
    assert stubbed(plan, pitches=short)["source"] == "template_fallback"
    assert stubbed(plan, pitches=[])["source"] == "template_fallback"
    swapped = route_pitches(plan)[::-1]
    assert stubbed(plan, pitches=swapped)["source"] == "template_fallback"


def test_malformed_json_falls_back_with_json_parse(plan):
    out = narrate(plan, OpenAINarrator(api_key=KEY, transport=stub(body=raw_reply("{not json"))))
    assert out["source"] == "template_fallback" and out["rejected_reason"] == "json_parse"


def test_key_scrubbed_from_city_pitches(plan, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    out = narrate(plan, OpenAINarrator(transport=stub(body=reply("Qloo shows audience affinity.", city_pitches=route_pitches(plan, f"Qloo shows audience affinity. {KEY}")))))
    assert KEY not in json.dumps(out)


# ----------------------------------------------------------------- brand cap and the word "eligible"


def _brand_names(plan):
    return [i["name"] for i in plan["sponsor_candidates"]["items"]]


def test_prompt_states_the_brand_cap_and_the_term():
    assert "at most four brands in the booking pitch and at most four in the summary" in nr.SYSTEM_PROMPT
    assert "'identified comedian'" in nr.SYSTEM_PROMPT and "never the word 'eligible'" in nr.SYSTEM_PROMPT
    assert "eligible comic" not in nr.SYSTEM_PROMPT


def test_booking_pitch_with_four_brands_passes_and_five_is_rejected(plan):
    names = _brand_names(plan)
    assert len(names) > 4
    ok = "Brands with audience overlap: " + ", ".join(names[:4]) + "."
    assert stubbed(plan, pitch=ok)["source"] == "openai"
    too_many = "Brands with audience overlap: " + ", ".join(names[:5]) + "."
    out = stubbed(plan, pitch=too_many)
    assert out["source"] == "template_fallback" and "too many brands" in out["rejected_reason"]
    assert not verify_narration(plan, too_many, summary="Qloo shows audience affinity.", booking_pitch=too_many)[0]


def test_summary_with_five_brands_is_rejected(plan):
    out = stubbed(plan, summary="Brands with audience overlap: " + ", ".join(_brand_names(plan)[:5]) + ".")
    assert out["source"] == "template_fallback" and "too many brands" in out["rejected_reason"]


def test_four_brands_in_each_of_summary_and_pitch_is_allowed(plan):
    names = _brand_names(plan)
    text = "Brands with audience overlap: " + ", ".join(names[:4]) + "."
    assert stubbed(plan, summary=text, pitch=text)["source"] == "openai"


@pytest.mark.parametrize("where", ["summary", "pitch", "city"])
def test_the_word_eligible_is_rejected(plan, where):
    text = "Qloo shows audience affinity; an eligible comic could be billed."
    kwargs = {"summary": text} if where == "summary" else {"pitch": text} if where == "pitch" else {"pitches": route_pitches(plan, text)}
    out = stubbed(plan, **kwargs)
    assert out["source"] == "template_fallback" and "eligible" in out["rejected_reason"]


def test_identified_comedian_term_is_accepted(plan):
    assert verify_narration(plan, "Qloo shows audience overlap with an identified comedian.")[0]
