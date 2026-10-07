"""Qloo access behind one interface: LiveClient (qloo CLI) and FixtureClient (saved JSON).

LiveClient never reads, stores, or prints an API key; the qloo CLI picks its
credentials up from the environment on its own. Cloud sessions have no Qloo
credential, so only the owner runs LiveClient, on his own machine.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

from .paths import fixture_folder

DEFAULT_TIMEOUT = 30.0  # a live call takes about 7 seconds
MAX_RETRIES = 2  # bounded: at most 3 attempts in total


class QlooError(RuntimeError):
    pass


class AmbiguousEntityError(QlooError):
    """Qloo returned status needs_input: the name must be resolved by a human."""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def city_slug(city: str) -> str:
    """'Austin, TX' -> 'austin' (the where_popular_<slug>.json suffix)."""
    return slugify(city.split(",")[0])


def build_chosen(candidates: list[dict[str, Any]], entity_id: str) -> dict[str, Any]:
    """The record saved as chosen.json: entity_id, name and description only (never tags).

    Fails loudly when the entity_id is not among the search candidates.
    """
    for e in candidates:
        if e.get("entity_id") == entity_id:
            return {
                "entity_id": entity_id,
                "name": e.get("name"),
                "description": (e.get("properties") or {}).get("short_description"),
            }
    raise ValueError(f"entity id {entity_id!r} is not in the search results")


class QlooClient(ABC):
    """Comedians are Qloo person entities; every call after search takes an entity ID."""

    @abstractmethod
    def search_person(self, name: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def where_popular(self, entity_id: str, city: str) -> dict[str, Any]: ...

    @abstractmethod
    def similar(self, entity_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def places(self, entity_id: str, city: str, category_tag: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def brands(self, entity_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    def person_description(self, entity_id: str) -> str | None: ...


class LiveClient(QlooClient):
    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = MAX_RETRIES,
        qloo_bin: str = "qloo",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.retries = min(retries, MAX_RETRIES)
        self.qloo_bin = qloo_bin
        self._sleep = sleep

    def _run(self, args: list[str]) -> Any:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                self._sleep(2.0 * attempt)
            try:
                proc = subprocess.run(
                    [self.qloo_bin, *args],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                last = QlooError(f"qloo {args[0]} timed out after {self.timeout}s")
                last.__cause__ = exc
                continue
            if proc.returncode != 0:
                # stderr is shown trimmed; the CLI does not echo credentials.
                last = QlooError(f"qloo {args[0]} failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
                continue
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                last = QlooError(f"qloo {args[0]} returned invalid JSON")
                last.__cause__ = exc
                continue
            if isinstance(data, dict) and data.get("status") == "needs_input":
                raise AmbiguousEntityError("Qloo needs a resolved entity ID, not a name")
            return data
        assert last is not None
        raise last

    def _exec(self, operation: str, payload: dict[str, Any]) -> Any:
        return self._run(["exec", operation, "--input", json.dumps(payload)])

    @staticmethod
    def _entities(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return data
        for key in ("results", "entities"):
            if isinstance(data, dict) and isinstance(data.get(key), list):
                return data[key]
        raise QlooError("unexpected response shape from qloo api")

    def search_person(self, name: str) -> list[dict[str, Any]]:
        data = self._run(["api", "search", "--query", name, "--type", "person", "--take", "3", "--json"])
        return self._entities(data)

    def where_popular(self, entity_id: str, city: str) -> dict[str, Any]:
        return self._exec("where_popular", {"entity": entity_id, "within": city})

    def similar(self, entity_id: str) -> list[dict[str, Any]]:
        # By entity ID, never by name: a name can resolve to the wrong entity.
        data = self._run(
            ["api", "insights", "--type", "person", "--signal-entities", entity_id, "--take", "10", "--json"]
        )
        return self._entities(data)

    def places(self, entity_id: str, city: str, category_tag: str) -> list[dict[str, Any]]:
        data = self._run(
            [
                "api", "insights", "--type", "place", "--signal-entities", entity_id,
                "--filter-location", city, "--filter-tags", category_tag, "--take", "10", "--json",
            ]
        )
        return self._entities(data)

    def brands(self, entity_id: str) -> list[dict[str, Any]]:
        data = self._run(
            ["api", "insights", "--type", "brand", "--signal-entities", entity_id, "--take", "10", "--json"]
        )
        return self._entities(data)

    def person_description(self, entity_id: str) -> str | None:
        """The one-line ``properties.short_description`` of a person, or None.

        Runs ``qloo api entity --id <id> --json`` and returns only that string. The full entity
        carries Wikipedia-style tags that can name sensitive traits; they are never read or kept.
        """
        data = self._run(["api", "entity", "--id", entity_id, "--json"])
        entity = data[0] if isinstance(data, list) and data else data
        if isinstance(data, dict):
            for key in ("results", "entities"):
                if isinstance(data.get(key), list):
                    entity = data[key][0] if data[key] else None
                    break
        if not isinstance(entity, dict):
            return None
        description = (entity.get("properties") or {}).get("short_description")
        return description if isinstance(description, str) and description.strip() else None


class FixtureClient(QlooClient):
    """Reads saved JSON from <root>/<slug>/ (search, chosen, openers, descriptions, brands, where_popular_*, places_*).

    ``root`` defaults to the private fixtures folder under ROADIE_DATA_DIR; tests pass tests/synthetic.
    """

    def __init__(self, slug: str, root: Path | None = None) -> None:
        self.dir = fixture_folder(slug, root)

    def _load(self, filename: str) -> Any:
        path = self.dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"no saved fixture: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def search_person(self, name: str) -> list[dict[str, Any]]:
        return self._load("search.json")

    def where_popular(self, entity_id: str, city: str) -> dict[str, Any]:
        return self._load(f"where_popular_{city_slug(city)}.json")

    def similar(self, entity_id: str) -> list[dict[str, Any]]:
        return self._load("openers.json")

    def places(self, entity_id: str, city: str, category_tag: str) -> list[dict[str, Any]]:
        return self._load(f"places_{city_slug(city)}.json")

    def brands(self, entity_id: str) -> list[dict[str, Any]]:
        return self._load("brands.json")

    def chosen(self) -> dict[str, Any] | None:
        """The comedian a human confirmed at capture time (chosen.json), or None when not saved."""
        path = self.dir / "chosen.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("entity_id"), str):
            raise QlooError(f"malformed {path}: expected an object with an entity_id")
        return data

    def descriptions(self) -> dict[str, str | None]:
        """entity_id -> one-line description from descriptions.json; empty when not captured yet."""
        path = self.dir / "descriptions.json"
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def person_description(self, entity_id: str) -> str | None:
        return self.descriptions().get(entity_id)
