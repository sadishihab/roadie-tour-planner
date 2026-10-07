import json
from pathlib import Path

import pytest

from roadie import rank_cities
from synthetic import JUNE, SOL, SYNTH_DIR

FIXTURES = SYNTH_DIR / JUNE


def _write(d: Path, name: str, city: str, cells):
    payload = {
        "operation": "where_popular",
        "status": "ok",
        "interpretation": {"within": city},
        "results": [{"query": {"affinity": a, "popularity": p}} for a, p in cells],
    }
    (d / f"where_popular_{name}.json").write_text(json.dumps(payload))


def test_synthetic_fixtures_rank_twelve_cities():
    ranked = rank_cities(FIXTURES)
    assert len(ranked) == 12
    assert [r.rank for r in ranked] == list(range(1, 13))
    assert [r.city for r in ranked[:3]] == ["Austin, TX", "Seattle, WA", "Chicago, IL"]
    assert ranked[-1].city == "Washington, DC"
    assert all(r.cell_count == 10 for r in ranked)
    assert ranked[0].avg_affinity == pytest.approx(0.99, abs=1e-6)


def test_synthetic_weak_evidence_flags():
    ranked = rank_cities(FIXTURES)
    flags = [r.weak_evidence for r in ranked]
    # gaps to the next city: five under 0.005, then 0.0055 (Boston/Denver), 0.0035, then 0.007 and wider
    assert flags == [True] * 5 + [False, True] + [False] * 5


def test_saturated_fixtures_flag_most_gaps():
    flags = [r.weak_evidence for r in rank_cities(SYNTH_DIR / SOL)]
    assert flags.count(True) >= 7  # affinity saturates near 1.0: most neighbours are near-ties
    assert flags[-1] is False  # the last city has no next city


def test_averages_and_clear_gap(tmp_path):
    _write(tmp_path, "a", "A, XX", [(0.9, 0.2), (0.7, 0.4)])
    _write(tmp_path, "b", "B, XX", [(0.5, 1.0), (0.5, 0.0)])
    a, b = rank_cities(tmp_path)
    assert (a.city, a.avg_affinity, a.avg_popularity, a.rank) == ("A, XX", pytest.approx(0.8), pytest.approx(0.3), 1)
    assert (b.city, b.rank) == ("B, XX", 2)
    assert not a.weak_evidence and not b.weak_evidence


def test_tie_broken_by_popularity_and_flagged(tmp_path):
    _write(tmp_path, "a", "A, XX", [(0.9, 0.1)])
    _write(tmp_path, "b", "B, XX", [(0.9, 0.8)])
    first, second = rank_cities(tmp_path)
    assert (first.city, second.city) == ("B, XX", "A, XX")
    assert first.weak_evidence and not second.weak_evidence


def test_custom_threshold(tmp_path):
    _write(tmp_path, "a", "A, XX", [(0.90, 0.1)])
    _write(tmp_path, "b", "B, XX", [(0.89, 0.1)])
    assert rank_cities(tmp_path)[0].weak_evidence is False
    assert rank_cities(tmp_path, weak_gap=0.05)[0].weak_evidence is True


def test_missing_files_raise(tmp_path):
    with pytest.raises(FileNotFoundError):
        rank_cities(tmp_path)


def test_rejects_non_ok_result(tmp_path):
    (tmp_path / "where_popular_x.json").write_text(json.dumps({"operation": "where_popular", "status": "needs_input"}))
    with pytest.raises(ValueError):
        rank_cities(tmp_path)
