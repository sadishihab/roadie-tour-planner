"""scripts/build_gallery.py: no network (no OPENAI_API_KEY), no secrets on disk."""

import importlib.util
import json
from pathlib import Path

import pytest

from synthetic import JUNE, SLUGS, SOL, SYNTH_DIR

spec = importlib.util.spec_from_file_location("build_gallery", Path(__file__).resolve().parents[2] / "scripts" / "build_gallery.py")
bg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bg)


@pytest.fixture(autouse=True)
def no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_builds_every_fixture_with_chosen_json(tmp_path):
    out = tmp_path / "g"
    results = bg.build_gallery(SYNTH_DIR, out)
    assert sorted(results) == SLUGS and set(results.values()) == {"written"}
    data = json.loads((out / f"{JUNE}.json").read_text())
    assert set(data) == {"slug", "name", "description", "plan", "narration", "built_with", "built_at"}
    assert data["slug"] == JUNE and data["name"] == "June Marlowe" and data["description"] == "Fictional stand-up comedian"
    assert data["built_with"] == "template" == data["narration"]["source"]
    assert data["plan"]["cities"] and data["narration"]["city_pitches"]


def test_folder_without_chosen_json_is_skipped(tmp_path):
    root = tmp_path / "fx"
    (root / "nobody").mkdir(parents=True)
    assert bg.build_gallery(root, tmp_path / "g") == {}


def test_folder_names_that_are_not_slugs_are_skipped(tmp_path):
    root = tmp_path / "fx"
    (root / "Not-A-Slug").mkdir(parents=True)
    (root / "Not-A-Slug" / "chosen.json").write_text("{}")
    assert bg.build_gallery(root, tmp_path / "g") == {}


def test_no_environment_value_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("ROADIE_CANARY", "canary-value-123")
    monkeypatch.setenv("QLOO_API_KEY", "canary-qloo-456")
    bg.build_gallery(SYNTH_DIR, tmp_path, only=[JUNE])
    text = (tmp_path / f"{JUNE}.json").read_text()
    assert "canary" not in text


def test_openai_file_is_not_replaced_by_template_without_force(tmp_path):
    target = tmp_path / f"{JUNE}.json"
    target.write_text(json.dumps({"built_with": "openai", "marker": 1}))
    assert bg.build_gallery(SYNTH_DIR, tmp_path, only=[JUNE]) == {JUNE: "kept"}
    assert json.loads(target.read_text())["marker"] == 1
    assert bg.build_gallery(SYNTH_DIR, tmp_path, force=True, only=[JUNE]) == {JUNE: "written"}
    assert json.loads(target.read_text())["built_with"] == "template"


def test_template_and_unreadable_files_are_overwritten(tmp_path):
    (tmp_path / f"{JUNE}.json").write_text(json.dumps({"built_with": "template"}))
    (tmp_path / f"{SOL}.json").write_text("garbage")
    results = bg.build_gallery(SYNTH_DIR, tmp_path, only=[JUNE, SOL])
    assert results == {SOL: "written", JUNE: "written"}


def test_default_folders_follow_roadie_data_dir(tmp_path, monkeypatch):
    """With no explicit folders the script reads <data>/fixtures and writes <data>/gallery."""
    import shutil

    monkeypatch.setenv("ROADIE_DATA_DIR", str(tmp_path / "private"))
    shutil.copytree(SYNTH_DIR / JUNE, tmp_path / "private" / "fixtures" / JUNE)
    assert bg.build_gallery() == {JUNE: "written"}
    assert (tmp_path / "private" / "gallery" / f"{JUNE}.json").is_file()
