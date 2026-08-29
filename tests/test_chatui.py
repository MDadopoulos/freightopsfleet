"""The chat page: self-contained, in step with the README, and mounted by devui."""

from __future__ import annotations

import re

from freight_fleet import chatui


def test_the_page_is_self_contained_and_escapes_its_env(monkeypatch):
    monkeypatch.setenv("FREIGHT_SANDBOX_URL", 'https://sb.test/?a="1"')
    monkeypatch.setenv("FREIGHT_PUBLIC_URL", "https://pub.test")
    html = chatui.page()
    assert "<title>Ask the fleet</title>" in html
    assert 'data-sandbox="https://sb.test/?a=&quot;1&quot;"' in html
    assert 'href="https://pub.test"' in html and 'href="/dev-ui/"' in html
    # no external requests of any kind: no src=, no href to a CDN, no @import
    assert not re.search(r'src="https?://', html) and "@import" not in html and "cdn" not in html.lower()
    assert "/run_sse" in html and "/users/me/sessions" in html


def test_starters_match_the_readme_table():
    """The README's eight questions are the ones on the page, in order."""
    from pathlib import Path

    readme = Path(__file__).resolve().parents[1].joinpath("README.md").read_text(encoding="utf-8")
    section = readme.split("## Ask the fleet", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| *") and not line.startswith("| **")]
    asked = [row.split("|")[1].strip().strip("*").split("* —")[0].strip() for row in rows]
    assert [a.rstrip("*").strip() for a in asked] == chatui.STARTERS


def test_devui_mounts_chat_when_the_app_is_fastapi(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from google.adk.cli import fast_api

    from freight_fleet import devui

    monkeypatch.setattr(fast_api, "get_fast_api_app", lambda **kw: FastAPI())
    monkeypatch.delenv("FREIGHT_IAP_AUDIENCE", raising=False)
    monkeypatch.delenv("FREIGHT_CHAT_ACCESS_CODE", raising=False)
    monkeypatch.delenv("FREIGHT_CHAT_USERS", raising=False)
    app = devui.create_app()
    r = TestClient(app).get("/chat")
    assert r.status_code == 200 and "Ask the fleet" in r.text
