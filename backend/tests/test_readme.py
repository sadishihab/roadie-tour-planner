"""The public README: required parts present, and nothing private or machine-specific in it."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")

KEY_LIKE = re.compile(r"sk-[A-Za-z0-9_-]{16,}|[A-Za-z0-9+/_-]{40,}|[0-9a-fA-F]{32,}")
LOCALHOST_PATH = re.compile(r"/home/|/Users/|/root/|C:\\\\|~/|file://")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
SESSION_LINK = re.compile(r"claude\.ai/code|session_[A-Za-z0-9]+", re.IGNORECASE)


def test_readme_has_no_localhost_path_email_session_link_or_key():
    assert not LOCALHOST_PATH.search(README)
    assert not EMAIL.search(README)
    assert not SESSION_LINK.search(README)
    assert not KEY_LIKE.search(README)
    assert not re.search(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}", README)


def test_readme_has_the_expected_sections_and_placeholders():
    for heading in ("## What it does", "## Why it needs Qloo", "## What we learned from the data",
                    "## Responsible by design", "## Run it", "## Data handling", "## Repository map", "## License"):
        assert heading in README, heading
    assert "DEMO_URL_PLACEHOLDER" in README  # replaced with the live URL after deployment
    assert "SCREENSHOTS_PLACEHOLDER" not in README  # the screenshots are in
    assert "## Screenshots" in README and "invented synthetic data" in README
    for name in ("01-gallery.png", "02-plan.png", "03-city.png"):
        assert f"docs/images/{name}" in README, name
    assert "Nov 16" in README
    assert "Work in progress" not in README
    assert len(README.splitlines()) <= 130


def test_readme_links_to_docs_that_exist():
    for target in re.findall(r"\]\((docs/[A-Za-z_.-]+\.md|LICENSE)\)", README):
        assert (ROOT / target).exists(), target


def test_docs_do_not_claim_the_image_was_never_built():
    text = " ".join((ROOT / "docs" / n).read_text(encoding="utf-8") for n in ("DEPLOY.md", "API.md", "ARCHITECTURE.md"))
    assert "not built" not in text and "no Docker daemon" not in text
    assert "133 seconds" in text
