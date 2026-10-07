"""Rank candidate cities from saved Qloo where_popular results (fixtures only, no network)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Affinity saturates near 1.0, so a gap below this between neighbours is a near-tie.
WEAK_GAP = 0.005


@dataclass(frozen=True)
class CityRanking:
    city: str
    avg_affinity: float
    avg_popularity: float
    rank: int
    weak_evidence: bool
    cell_count: int


def _load_city(path: Path) -> tuple[str, float, float, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("operation") != "where_popular" or data.get("status") != "ok":
        raise ValueError(f"{path}: not an ok where_popular result")
    city = data["interpretation"]["within"]
    cells = data["results"]
    if not cells:
        raise ValueError(f"{path}: no result cells")
    n = len(cells)
    affinity = sum(c["query"]["affinity"] for c in cells) / n
    popularity = sum(c["query"]["popularity"] for c in cells) / n
    return city, affinity, popularity, n


def rank_cities(fixture_dir: str | Path, weak_gap: float = WEAK_GAP) -> list[CityRanking]:
    """Rank cities by average affinity (ties: average popularity, then name).

    Reads every ``where_popular_*.json`` in ``fixture_dir``. A city is flagged
    ``weak_evidence`` when its affinity gap to the next-ranked city is below
    ``weak_gap``; the last city has no next city and is never flagged.
    """
    paths = sorted(Path(fixture_dir).glob("where_popular_*.json"))
    if not paths:
        raise FileNotFoundError(f"no where_popular_*.json files in {fixture_dir}")
    rows = sorted((_load_city(p) for p in paths), key=lambda r: (-r[1], -r[2], r[0]))
    result = []
    for i, (city, aff, pop, n) in enumerate(rows):
        weak = i + 1 < len(rows) and (aff - rows[i + 1][1]) < weak_gap
        result.append(CityRanking(city, aff, pop, i + 1, weak, n))
    return result
