"""Pipeline: turn saved Qloo fixtures into a structured tour plan for a touring comedian.

No LLM and no network: plan_tour only reads through a FixtureClient. Every item
carries ``source``: ``qloo_result`` (a value Qloo returned) or
``roadie_interpretation`` (something Roadie derived). Wording stays at the level
of aggregate audience affinity; nothing here claims anything about individuals
or causes.

Sections cut from the earlier version (vibe tags, shared audience, trends): they
were cut because entity_tags and compare_audiences returned thin or tiered data
for comedians.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .cities import CITY_COORDS
from .qloo_client import AmbiguousEntityError, FixtureClient, QlooError, slugify
from .ranking import rank_cities

QLOO = "qloo_result"
ROADIE = "roadie_interpretation"

COMEDY_CLUB_TAG = "urn:tag:category:place:comedy_club"
# A comic within this popularity distance of the comedian counts as a peer.
PEER_BAND = 0.05
MIN_VENUES = 5
# Tier cutoffs on avg_affinity, measured as the gap below the top city. These are Roadie's own
# heuristic, not Qloo values: where_popular affinity saturates near 1.0, so a 1-to-12 ranking
# overstates what the data says, and Roadie groups cities into coarse tiers instead.
STRONG_GAP = 0.010
MODERATE_GAP = 0.020
_EPS = 1e-9  # float slack so a gap of exactly the cutoff stays in the tier
TIER_ORDER = ("strong", "moderate", "weaker")

CELL_NOTE = (
    "The score summarizes the strongest neighborhoods in the city: where_popular returns "
    "neighborhood-level map cells and Roadie averages the top 10."
)
TIER_WORDING = {
    "strong": "strong audience affinity",
    "moderate": "moderate audience affinity",
    "weaker": "weaker audience affinity",
}
TIER_NOTE = (
    "Tiers group cities by how close their average audience affinity is to the top candidate "
    f"(strong: within {STRONG_GAP:.3f}, moderate: within {MODERATE_GAP:.3f}, otherwise weaker). "
    "Affinity saturates near 1.0, so cities in the same tier cannot be meaningfully ordered "
    "against each other; the rank number is kept only as the raw sort order."
)
NOT_CAPTURED_NOTE = "not captured"
MAX_DESCRIPTION = 240
ROUTE_RULE = (
    "strong-tier cities sorted by longitude from west to east, using approximate city-center coordinates"
)
ROUTE_NOTE = (
    "A suggested geographic order only, not a ranking: it covers every strong-tier city and sorts them "
    "by longitude from west to east, using approximate city-center coordinates. It ignores travel "
    "logistics, dates and routing constraints."
)
COMICS_NOTE = (
    "Qloo returns people with overlapping audiences, not necessarily comedians. Roadie marks a person as "
    "an identified comedian when their one-line Qloo description contains the word comedian or comic; this "
    "is a simple text match on a short description and can miss real comics. Identified comedians are "
    "listed first. The value is null (unknown) when descriptions were not captured."
)
COMICS_NO_DESCRIPTIONS = " Descriptions have not been captured for this comedian, so no one is identified yet."
# Simple text match on a one-line description, not real identification: it can miss real comics
# (a description such as "Podcaster" or "Actor") and can match non-comics ("comic book artist").
_COMEDIAN_WORDS = re.compile(r"\b(comedian|comic)s?\b", re.IGNORECASE)
VENUES_LIMITS = (
    "Qloo gives audience affinity for a place, not capacity, booking policy, or the difference "
    "between stand-up, improv and sketch rooms. Treat these as places to research, not confirmed fits."
)
SPONSORS_LIMITS = (
    "Qloo shows audience overlap, not sponsorship intent. Treat these as brands to research, "
    "not as likely sponsors."
)


def assign_tier(avg_affinity: float, top_affinity: float) -> str:
    """Tier by the gap below the top city's average affinity (Roadie's heuristic)."""
    gap = top_affinity - avg_affinity
    if gap <= STRONG_GAP + _EPS:
        return "strong"
    if gap <= MODERATE_GAP + _EPS:
        return "moderate"
    return "weaker"


def city_interpretation(tier: str) -> str:
    return f"{TIER_WORDING[tier].capitalize()} relative to the other candidate cities. {CELL_NOTE}"


@dataclass
class StepEvent:
    step: str
    queried: str
    summary: str
    source: str


@dataclass
class TourPlan:
    slug: str
    comedian: dict[str, Any]
    cities: list[dict[str, Any]]
    city_tier_note: dict[str, str]
    route_suggestion: dict[str, Any]
    comics_to_bill: dict[str, Any]
    comedy_venues: dict[str, Any]
    sponsor_candidates: dict[str, Any]
    steps: list[StepEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resolve_comedian(slug: str, client: FixtureClient) -> dict[str, Any]:
    """Use the human's saved choice (chosen.json); only without one, match the slug against names."""
    candidates = client.search_person(slug)
    chosen = client.chosen()
    if chosen is not None:
        matches = [c for c in candidates if c.get("entity_id") == chosen["entity_id"]]
        if len(matches) != 1:
            raise QlooError(f"chosen entity {chosen['entity_id']!r} is not in the search results for {slug!r}")
    else:
        matches = [c for c in candidates if slugify(c.get("name", "")) == slug]
        if len(matches) != 1:
            raise AmbiguousEntityError(f"{slug!r} matches {len(matches)} search results; a human must choose")
    hit = matches[0]
    return {"name": hit["name"], "entity_id": hit["entity_id"], "popularity": hit.get("popularity")}


def classify_comic(comic_pop: float, comedian_pop: float, band: float = PEER_BAND) -> str:
    if comic_pop > comedian_pop + band:
        return "bigger act"
    if comic_pop < comedian_pop - band:
        return "smaller act"
    return "peer"


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance (haversine); approximate is enough for ordering."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def route_length_km(order: list[str]) -> float:
    """Total great-circle length of visiting ``order`` in sequence."""
    return sum(distance_km(CITY_COORDS[a], CITY_COORDS[b]) for a, b in zip(order, order[1:]))


def suggest_route(cities: list[str]) -> list[str]:
    """A west-to-east sweep: ``cities`` sorted by longitude (name breaks ties)."""
    missing = [c for c in cities if c not in CITY_COORDS]
    if missing:
        raise KeyError(f"no coordinates for {missing}")
    return sorted(cities, key=lambda c: (CITY_COORDS[c][1], c))


def identified_as_comedian(description: str | None, captured: bool) -> bool | None:
    """True/False from a text match on the description; None (unknown) when descriptions are not captured."""
    if not captured:
        return None
    return bool(description and _COMEDIAN_WORDS.search(description))


def _short(text: Any) -> str | None:
    """Qloo text is untrusted input: accept strings only and cap the length."""
    if not isinstance(text, str) or not text.strip():
        return None
    text = " ".join(text.split())
    return text if len(text) <= MAX_DESCRIPTION else text[: MAX_DESCRIPTION - 1].rstrip() + "…"


def _insight_items(entities: list[dict[str, Any]], signal: str, interpretation: str) -> list[dict[str, Any]]:
    items = []
    for rank, e in enumerate(entities, 1):
        item = {
            "rank": rank,
            "name": e["name"],
            "entity_id": e["entity_id"],
            "affinity": round(e["query"]["affinity"], 4),
            "popularity": round(e["popularity"], 4),
            "source": QLOO,
            "signal": signal,
            "interpretation": {"text": interpretation, "source": ROADIE},
        }
        description = _short((e.get("properties") or {}).get("short_description"))
        if description:
            item["description"] = description
        items.append(item)
    return items


def _note(text: str) -> dict[str, str]:
    return {"text": text, "source": ROADIE}


def plan_tour(
    slug: str,
    client: FixtureClient,
    on_step: Callable[[StepEvent], None] | None = None,
) -> TourPlan:
    if not isinstance(client, FixtureClient):
        raise TypeError("plan_tour is fixture-only: pass a FixtureClient")
    steps: list[StepEvent] = []

    def emit(step: str, queried: str, summary: str, source: str) -> None:
        ev = StepEvent(step, queried, summary, source)
        steps.append(ev)
        if on_step:
            on_step(ev)

    # 1. resolve (the name is only matched against the slug; later calls use the ID)
    comedian = _resolve_comedian(slug, client)
    cid = comedian["entity_id"]
    cpop = comedian["popularity"]
    emit("resolve_comedian", f"search person {comedian['name']!r}", f"resolved to entity {cid}", QLOO)

    # 2. cities
    ranked = rank_cities(client.dir)
    top_affinity = ranked[0].avg_affinity
    tiers = {r.city: assign_tier(r.avg_affinity, top_affinity) for r in ranked}
    cities = [
        {
            "rank": r.rank,
            "city": r.city,
            "tier": tiers[r.city],
            "tier_source": ROADIE,
            "avg_affinity": round(r.avg_affinity, 4),
            "avg_popularity": round(r.avg_popularity, 4),
            "cell_count": r.cell_count,
            "source": QLOO,
            "signal": "where_popular within city, averaged over the top 10 neighborhood-level map cells",
            "interpretation": {"text": city_interpretation(tiers[r.city]), "source": ROADIE},
        }
        for r in ranked
    ]
    counts_by_tier = {t: sum(v == t for v in tiers.values()) for t in TIER_ORDER}
    emit(
        "rank_cities",
        f"where_popular for {len(ranked)} candidate cities",
        f"top: {ranked[0].city}; tiers {counts_by_tier}",
        ROADIE,
    )

    # 3. route suggestion over every strong-tier city: a west-to-east sweep by longitude
    strong = [r.city for r in ranked if tiers[r.city] == "strong"]
    order = suggest_route(strong)
    route = {
        "label": f"suggested geographic order of the {len(strong)} strong-tier cities",
        "rule": ROUTE_RULE,
        "order": [
            {"step": i, "city": c, "tier": "strong", "source": ROADIE}
            for i, c in enumerate(order, 1)
        ],
        "note": _note(ROUTE_NOTE),
        "source": ROADIE,
    }
    emit("route_suggestion", f"west-to-east sweep over {len(strong)} strong-tier cities by longitude", " -> ".join(order), ROADIE)

    # 4. comics to bill with (dropping the comedian itself, by ID)
    descriptions = client.descriptions()
    captured = bool(descriptions)  # no descriptions.json yet: identification is unknown, not false
    comics = []
    for o in client.similar(cid):
        if o.get("entity_id", "").upper() == cid.upper():
            continue
        rel = classify_comic(o["popularity"], cpop)
        description = _short(descriptions.get(o["entity_id"]))
        known = captured and o["entity_id"] in descriptions
        comic = {
            "name": o["name"],
            "entity_id": o["entity_id"],
            "affinity": round(o["query"]["affinity"], 4),
            "popularity": round(o["popularity"], 4),
            "relation": rel,
            "identified_as_comedian": identified_as_comedian(description, known),
            "identified_source": ROADIE,
            "source": QLOO,
            "signal": "insights type person with the comedian as signal entity",
            "interpretation": {
                "text": f"{rel} compared with the comedian's popularity ({round(cpop, 2)}), band ±{PEER_BAND}",
                "source": ROADIE,
            },
        }
        if description:
            comic["description"] = description
        comics.append(comic)
    comics.sort(key=lambda c: c["identified_as_comedian"] is not True)  # stable: identified first
    identified = sum(c["identified_as_comedian"] is True for c in comics)
    counts = {k: sum(c["relation"] == k for c in comics) for k in ("peer", "bigger act", "smaller act")}
    comics_section = {
        "label": "people with overlapping audiences, identified comedians first",
        "items": comics,
        "note": _note(COMICS_NOTE + ("" if captured else COMICS_NO_DESCRIPTIONS)),
        "source": ROADIE,
    }
    emit(
        "find_comics_to_bill",
        "insights type person by entity ID",
        f"{len(comics)} candidates: {counts}; identified as comedian: {identified if captured else 'unknown'}",
        QLOO,
    )

    # 5. comedy venues for every route (strong-tier) city
    venue_cities = []
    for city in order:
        try:
            places = client.places(cid, city, COMEDY_CLUB_TAG)
        except FileNotFoundError:
            places = []
        entry: dict[str, Any] = {
            "city": city,
            "label": f"comedy venues this audience favors in {city}",
            "items": _insight_items(
                places,
                f"insights type place, filter-location {city}, filter-tags {COMEDY_CLUB_TAG}, comedian as signal entity",
                f"high audience affinity in {city}; says nothing about capacity or availability",
            ),
        }
        if not places:
            entry["note"] = NOT_CAPTURED_NOTE
        if len(places) < MIN_VENUES:
            entry["limited_data_note"] = _note(
                f"Limited data: Qloo returned only {len(places)} comedy venues for {city} "
                f"(fewer than {MIN_VENUES}), so this list may be incomplete."
            )
        venue_cities.append(entry)
    venues = {"cities": venue_cities, "limits": _note(VENUES_LIMITS)}
    emit(
        "comedy_venues",
        f"insights type place with {COMEDY_CLUB_TAG} in {len(order)} route cities",
        ", ".join(f"{v['city']}: {len(v['items'])}" for v in venue_cities),
        QLOO,
    )

    # 6. sponsor candidates: brands with audience overlap
    try:
        brands = client.brands(cid)
    except FileNotFoundError:
        brands = []
    sponsors: dict[str, Any] = {
        "label": "brands with audience overlap worth approaching",
        "items": _insight_items(
            brands,
            "insights type brand, comedian as signal entity",
            "audience overlap with the comedian; says nothing about sponsorship intent",
        ),
        "limits": _note(SPONSORS_LIMITS),
    }
    if not brands:
        sponsors["note"] = NOT_CAPTURED_NOTE
    emit("sponsor_candidates", "insights type brand", f"{len(sponsors['items'])} brands", QLOO)

    return TourPlan(slug, {**comedian, "source": QLOO}, cities, _note(TIER_NOTE), route, comics_section, venues, sponsors, steps)
