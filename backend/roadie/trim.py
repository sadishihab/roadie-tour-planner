"""Trim raw ``qloo api insights --json`` entities down to the fields Roadie reads.

A raw place or brand entity carries hours, phones, image URLs, locality trees and
dozens of long generated descriptions (about 30 KB each). The pipeline only needs
the name, id, types, popularity, affinity, one short description and tag names.
The top-level shape is unchanged (a list of entities, or a dict with a
``results``/``entities`` list), and kept fields stay at their original paths
(``query.affinity``, ``properties.short_description``), so existing readers work.

Person entities are trimmed harder and never keep tags. Qloo's person tags are
Wikipedia-style categories about named individuals (they can name sexual orientation,
religion or ethnicity), and Qloo's safe-use rules say not to infer or surface sensitive
traits, so they must not sit in a public repo. Only name, entity_id, type, popularity,
affinity and the one-line description are kept for a person.
"""

from __future__ import annotations

from typing import Any

KEPT_KEYS = ("name", "entity_id", "type", "subtype", "popularity")
PERSON_KEPT_KEYS = ("name", "entity_id", "type", "popularity")
LIST_KEYS = ("results", "entities")
PERSON_URN = "urn:entity:person"


def is_person(entity: dict[str, Any]) -> bool:
    """True for a person entity (search results use ``types``, insights use ``subtype``)."""
    types = entity.get("types")
    return entity.get("subtype") == PERSON_URN or (isinstance(types, list) and PERSON_URN in types)


def trim_person(entity: dict[str, Any]) -> dict[str, Any]:
    """Keep name, entity_id, type, popularity, affinity and short_description. Never tags."""
    out = {k: entity[k] for k in PERSON_KEPT_KEYS if k in entity}
    query = entity.get("query")
    if isinstance(query, dict) and "affinity" in query:
        out["query"] = {"affinity": query["affinity"]}
    description = (entity.get("properties") or {}).get("short_description")
    if isinstance(description, str) and description:
        out["properties"] = {"short_description": description}
    return out


def trim_entity(entity: dict[str, Any]) -> dict[str, Any]:
    if is_person(entity):
        return trim_person(entity)
    out = {k: entity[k] for k in KEPT_KEYS if k in entity}
    query = entity.get("query")
    if isinstance(query, dict) and "affinity" in query:
        out["query"] = {"affinity": query["affinity"]}
    description = (entity.get("properties") or {}).get("short_description")
    if isinstance(description, str) and description:
        out["properties"] = {"short_description": description}
    tags = entity.get("tags")
    if isinstance(tags, list):
        out["tags"] = [{"name": t["name"]} for t in tags if isinstance(t, dict) and "name" in t]
    return out


def trim_payload(data: Any) -> Any:
    """Trim every entity in a saved insights response, keeping its top-level shape."""
    if isinstance(data, list):
        return [trim_entity(e) for e in data]
    if isinstance(data, dict):
        for key in LIST_KEYS:
            if isinstance(data.get(key), list):
                return {**data, key: [trim_entity(e) for e in data[key]]}
    raise ValueError("unexpected insights shape: expected a list or a dict with results/entities")
