"""Deployment files: Dockerfile, .dockerignore, render.yaml and the container entrypoint.

No Docker build and no network. The entrypoint runs under /bin/sh with stub ``python`` and ``qloo``
programs on PATH, against temporary folders; the key used here is an obvious placeholder.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "scripts" / "docker-entrypoint.sh"
PLACEHOLDER_KEY = "placeholder-not-a-real-key"
KEY_LIKE = re.compile(r"sk-[A-Za-z0-9_-]{16,}|[A-Za-z0-9+/_-]{40,}|[0-9a-fA-F]{32,}")


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _lines(name: str) -> list[str]:
    return [ln.strip() for ln in _text(name).splitlines() if ln.strip() and not ln.strip().startswith("#")]


# ------------------------------------------------------------------------------ entrypoint


@pytest.fixture
def box(tmp_path):
    """Stub python (records its arguments) and qloo (records its arguments and whether a key was visible)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "python": 'echo "$@" > "$STUB_LOG_DIR/python_args"\n'
                  'echo "BASE=${QLOO_BASE_URL:-} TRUSTED=${QLOO_TRUSTED_BASE_URL:-}" > "$STUB_LOG_DIR/python_env"\n',
        "qloo": 'echo "$@" > "$STUB_LOG_DIR/qloo_args"\necho "key=${QLOO_API_KEY:-unset}" > "$STUB_LOG_DIR/qloo_key"\n'
                'echo "stub qloo output with ${QLOO_BASE_URL:-nothing}"\n',
    }.items():
        path = bin_dir / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    logs = tmp_path / "logs"
    logs.mkdir()
    return {"bin": bin_dir, "logs": logs, "data": tmp_path / "data", "secrets": tmp_path / "secrets", "tmp": tmp_path}


def run_entry(box, secrets=True, extra_env=None, with_qloo=True):
    env = {
        "PATH": f"{box['bin']}:/usr/bin:/bin" if with_qloo else f"{box['bin']}-nope:/usr/bin:/bin",
        "ROADIE_DATA_DIR": str(box["data"]),
        "STUB_LOG_DIR": str(box["logs"]),
        "QLOO_API_KEY": PLACEHOLDER_KEY,
    }
    if not with_qloo:  # python stub still needed
        stub_only = box["tmp"] / "pybin"
        stub_only.mkdir(exist_ok=True)
        (stub_only / "python").write_text((box["bin"] / "python").read_text())
        (stub_only / "python").chmod(0o755)
        env["PATH"] = f"{stub_only}:/usr/bin:/bin"
    if secrets:
        env["ROADIE_SECRETS_DIR"] = str(box["secrets"])
    else:
        env["ROADIE_SECRETS_DIR"] = str(box["tmp"] / "no-such-folder")
    env.update(extra_env or {})
    return subprocess.run(["/bin/sh", str(ENTRYPOINT)], env=env, capture_output=True, text=True, timeout=30)


def gallery_files(box) -> set[str]:
    folder = box["data"] / "gallery"
    return {p.name for p in folder.iterdir()} if folder.exists() else set()


def test_entrypoint_copies_only_safe_names_and_prints_a_count(box):
    box["secrets"].mkdir()
    for name in ("alpha.json", "beta_2.json", "x" * 40 + ".json"):
        (box["secrets"] / name).write_text('{"name": "synthetic"}')
    for bad in ("Alpha.json", "alpha.txt", "alpha.json.bak", "..json", "a-b.json", "x" * 41 + ".json", ".hidden.json", "my file.json"):
        (box["secrets"] / bad).write_text("{}")
    (box["secrets"] / "folder.json").mkdir()
    r = run_entry(box)
    assert r.returncode == 0, r.stderr
    assert gallery_files(box) == {"alpha.json", "beta_2.json", "x" * 40 + ".json"}
    assert (box["data"] / "gallery" / "alpha.json").read_text() == '{"name": "synthetic"}'
    assert "gallery files copied: 3" in r.stdout
    for name in ("alpha", "beta_2", "Alpha", "folder", "my file"):  # never a file name
        assert name not in r.stdout + r.stderr
    assert PLACEHOLDER_KEY not in r.stdout + r.stderr


def test_entrypoint_copies_never_symlinks(box):
    box["secrets"].mkdir()
    (box["secrets"] / "alpha.json").write_text("{}")
    r = run_entry(box)
    assert r.returncode == 0
    copy = box["data"] / "gallery" / "alpha.json"
    assert copy.is_file() and not copy.is_symlink()


def test_entrypoint_ignores_a_symlink_to_a_directory_and_traversal_names(box):
    box["secrets"].mkdir()
    outside = box["tmp"] / "outside"
    outside.mkdir()
    (box["secrets"] / "linked.json").symlink_to(outside, target_is_directory=True)
    (box["secrets"] / "..%2f..%2fevil.json").write_text("{}")
    (box["secrets"] / "-n.json").write_text("{}")
    r = run_entry(box)
    assert r.returncode == 0
    assert gallery_files(box) == set()
    assert not (box["tmp"] / "evil.json").exists()
    assert "no gallery files found" in r.stdout


def test_entrypoint_with_only_bad_names_copies_nothing_and_still_starts(box):
    box["secrets"].mkdir()
    (box["secrets"] / "..escape.json").write_text("{}")
    (box["secrets"] / "notes.txt").write_text("hi")
    r = run_entry(box)
    assert r.returncode == 0 and gallery_files(box) == set()
    assert (box["logs"] / "python_args").exists()  # uvicorn was started


def test_entrypoint_without_a_secrets_folder_starts_cleanly(box):
    r = run_entry(box, secrets=False)
    assert r.returncode == 0, r.stderr
    assert (box["data"] / "gallery").is_dir() and gallery_files(box) == set()
    assert r.stdout.count("no gallery files found") == 1 and "secrets folder is missing" in r.stdout
    assert "-m uvicorn roadie.api:app" in (box["logs"] / "python_args").read_text()


def _only_line(r) -> str:
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("roadie: ") and ("gallery" in ln or "secrets folder" in ln)]
    assert len(lines) == 1, r.stdout
    return lines[0]


def test_entrypoint_reports_a_readable_folder_with_a_count(box):
    box["secrets"].mkdir()
    (box["secrets"] / "alpha.json").write_text("{}")
    r = run_entry(box)
    assert r.returncode == 0 and "gallery files copied: 1" in _only_line(r)
    assert "not readable" not in r.stdout and "folder is missing" not in r.stdout and "folder is empty" not in r.stdout


def test_entrypoint_reports_an_empty_folder_as_empty(box):
    box["secrets"].mkdir()
    r = run_entry(box)
    line = _only_line(r)
    assert r.returncode == 0 and "no gallery files found" in line and "secrets folder is empty" in line
    assert "not readable" not in line and "missing" not in line


def test_entrypoint_reports_a_missing_folder_as_missing(box):
    r = run_entry(box, secrets=False)
    line = _only_line(r)
    assert r.returncode == 0 and "secrets folder is missing" in line and "not readable" not in line and "folder is empty" not in line


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read any folder")
def test_entrypoint_reports_an_unreadable_folder_and_still_starts(box):
    box["secrets"].mkdir()
    (box["secrets"] / "alpha.json").write_text('{"name": "synthetic"}')
    box["secrets"].chmod(0o000)
    try:
        r = run_entry(box)
    finally:
        box["secrets"].chmod(0o700)  # so pytest can clean the temporary folder up
    line = _only_line(r)
    assert r.returncode == 0, r.stderr
    assert "exists but is not readable by this user" in line
    assert "no gallery files found" not in r.stdout and "entries skipped" not in r.stdout
    assert "alpha" not in r.stdout + r.stderr and str(box["secrets"]) not in r.stdout + r.stderr  # no names, no paths
    assert PLACEHOLDER_KEY not in r.stdout + r.stderr
    assert gallery_files(box) == set()
    assert "-m uvicorn roadie.api:app" in (box["logs"] / "python_args").read_text()  # the app still starts


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can read any folder")
def test_entrypoint_treats_a_listable_but_not_enterable_folder_as_unreadable(box):
    box["secrets"].mkdir()
    (box["secrets"] / "alpha.json").write_text("{}")
    box["secrets"].chmod(0o400)  # names can be listed, files cannot be opened
    try:
        r = run_entry(box)
    finally:
        box["secrets"].chmod(0o700)
    assert "exists but is not readable by this user" in r.stdout and gallery_files(box) == set()


def test_entrypoint_starts_one_worker_on_port_and_all_interfaces(box):
    run_entry(box, extra_env={"PORT": "10000"})
    args = (box["logs"] / "python_args").read_text()
    assert "--host 0.0.0.0" in args and "--port 10000" in args and "--workers 1" in args
    run_entry(box, extra_env={"PORT": "not-a-port; rm -rf /"})
    assert "--port 8000" in (box["logs"] / "python_args").read_text()
    (box["logs"] / "python_args").unlink()
    run_entry(box)
    assert "--port 8000" in (box["logs"] / "python_args").read_text()


def test_entrypoint_trusts_the_hackathon_endpoint_by_default_and_lets_the_operator_override(box):
    r = run_entry(box)
    assert "BASE=https://hackathon.api.qloo.com TRUSTED=https://hackathon.api.qloo.com" in (box["logs"] / "python_env").read_text()
    assert (box["logs"] / "qloo_args").read_text().strip() == "config set base-url https://hackathon.api.qloo.com"
    run_entry(box, extra_env={"QLOO_BASE_URL": "https://override.example", "QLOO_TRUSTED_BASE_URL": "https://override.example"})
    assert "BASE=https://override.example TRUSTED=https://override.example" in (box["logs"] / "python_env").read_text()
    assert r.returncode == 0


def test_entrypoint_never_passes_the_key_to_config_set_or_prints_anything_from_it(box):
    r = run_entry(box)
    assert (box["logs"] / "qloo_key").read_text().strip() == "key=unset"  # config set runs without the key
    assert PLACEHOLDER_KEY not in r.stdout + r.stderr
    assert "stub qloo output" not in r.stdout + r.stderr  # the harness output is discarded
    assert not any(PLACEHOLDER_KEY in p.read_text() for p in box["tmp"].rglob("*") if p.is_file() and p.name != "python_env")


def test_entrypoint_starts_without_the_harness_installed(box):
    r = run_entry(box, with_qloo=False)
    assert r.returncode == 0 and "BASE=https://hackathon.api.qloo.com" in (box["logs"] / "python_env").read_text()


def test_entrypoint_is_posix_sh_and_never_echoes_environment_values():
    text = _text("scripts/docker-entrypoint.sh")
    assert text.startswith("#!/bin/sh") and "set -eu" in text
    assert not re.search(r"\b(env|printenv|set)\s*(\||$)", text, re.M)
    assert "ln -s" not in text
    assert PLACEHOLDER_KEY not in text and "QLOO_API_KEY" in text  # only to unset it for config set


# ------------------------------------------------------------------------------ Dockerfile


def test_dockerfile_has_no_key_in_env_or_arg_and_no_data_copy():
    lines = _lines("Dockerfile")
    env_arg = " ".join(ln for ln in lines if re.match(r"(ENV|ARG)\b", ln, re.I))
    assert env_arg  # ROADIE_DATA_DIR and friends
    assert not re.search(r"KEY|TOKEN|SECRET|PASSWORD", env_arg, re.I)
    assert not KEY_LIKE.search("\n".join(lines))
    for ln in lines:
        if re.match(r"(COPY|ADD)\b", ln, re.I):
            assert not re.search(r"\b(data|fixtures|gallery|tests?|\.git)\b", ln.split("--from=")[0], re.I), ln
            assert not re.match(r"(COPY|ADD)\s+(--\S+\s+)*\.\s", ln, re.I), ln  # never the whole context


def test_dockerfile_pins_runtimes_and_the_harness_and_runs_non_root():
    text = _text("Dockerfile")
    assert re.search(r"FROM python:3\.(1[2-9]|[2-9]\d)", text)
    assert re.search(r"FROM node:2[2-9]\b", text)  # the harness needs Node 22.19+, checked in the build too
    assert "@qloo/qloo-harness@0.1.26" in text
    assert re.search(r"^USER roadie$", text, re.M)
    assert "HEALTHCHECK" in text and "/api/health" in text
    assert "curl" not in "\n".join(_lines("Dockerfile")).lower()
    assert "[dev]" not in text and "pytest" not in text
    assert 'ENTRYPOINT ["/bin/sh", "/app/scripts/docker-entrypoint.sh"]' in text
    assert "ROADIE_SECRETS_DIR=/etc/secrets" in text and "ROADIE_DATA_DIR=/app/data" in text


def test_dockerignore_excludes_private_data_tests_and_caches():
    entries = set(_lines(".dockerignore"))
    for needed in ("data", "fixtures", "gallery", ".venv", ".git", "tests", "**/__pycache__", ".pytest_cache"):
        assert needed in entries, needed
    assert not {"scripts", "backend", "frontend"} & entries  # the image needs these


# ------------------------------------------------------------------------------ render.yaml


def test_render_yaml_keeps_the_key_out_of_the_repo():
    text = _text("render.yaml")
    assert re.search(r"-\s*key:\s*QLOO_API_KEY\s*\n\s*sync:\s*false", text)
    assert not KEY_LIKE.search("\n".join(_lines("render.yaml")))
    assert not re.search(r"OPENAI", text)
    assert "Secret Files" in text and "1 MB" in text


def test_render_yaml_trusts_one_proxy_hop_by_default():
    text = _text("render.yaml")
    assert "ROADIE_TRUSTED_PROXY_HOPS" not in text  # the code default (1) applies; DEPLOY.md says to verify it


def test_render_yaml_defaults():
    text = _text("render.yaml")
    assert re.search(r"runtime:\s*docker", text) and re.search(r"plan:\s*free", text)
    assert re.search(r"healthCheckPath:\s*/api/health", text)
    pairs = dict(re.findall(r"-\s*key:\s*(\w+)\s*\n\s*value:\s*\"?([^\"\n]+)\"?", text))
    assert pairs == {
        "ROADIE_LIVE": "1",
        "ROADIE_LIVE_UNTIL": "2026-11-16",
        "ROADIE_GLOBAL_SEARCHES_PER_DAY": "50",
        "ROADIE_TRUST_PROXY": "1",
        "QLOO_BASE_URL": "https://hackathon.api.qloo.com",
        "QLOO_TRUSTED_BASE_URL": "https://hackathon.api.qloo.com",
    }


def test_deploy_docs_exist_and_hold_no_key_like_value():
    text = _text("docs/DEPLOY.md")
    for needed in ("docker build", "docker run", "QLOO_API_KEY", "/etc/secrets", "/api/health", "Secret Files"):
        assert needed in text, needed
    assert "-e QLOO_API_KEY" in text and "QLOO_API_KEY=" not in text  # read from the shell, never a literal
    assert not KEY_LIKE.search(text)
    assert os.path.exists(ROOT / "render.yaml")
