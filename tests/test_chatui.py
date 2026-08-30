"""The chat page: self-contained, wearing the console's nav, in step with the README."""

from __future__ import annotations

import re

from freight_fleet import chatui


def test_the_page_is_self_contained_and_wears_the_console_nav(monkeypatch, tmp_path):
    monkeypatch.setenv("FREIGHT_LEDGER_PATH", str(tmp_path / "none.jsonl"))
    monkeypatch.setenv("FREIGHT_APPROVALS_PATH", str(tmp_path / "none.json"))
    monkeypatch.setenv("FREIGHT_CHAT_URL", "/chat")
    monkeypatch.setenv("FREIGHT_GATED", "1")
    monkeypatch.delenv("FREIGHT_PRICE_IN_PER_M", raising=False)
    html = chatui.page(identity='judge<1>@x.test')
    assert "<title>Ask the fleet</title>" in html
    # the console's nav, with this page marked current and a way out
    assert 'href="/desk"' in html and 'href="/ledger"' in html and 'href="/sent"' in html
    assert 'href="/chat" aria-current="page"' in html and 'href="/logout"' in html
    # the identity is shown back, escaped
    assert "judge&lt;1&gt;@x.test" in html and "judge<1>" not in html
    # no external requests of any kind: no src=, no href to a CDN, no @import
    assert not re.search(r'src="https?://', html) and "@import" not in html and "cdn" not in html.lower()
    assert "/run_sse" in html and "/users/me/sessions" in html
    # the upload control and the usage panel are on the page; prices are opt-in
    assert 'id="file"' in html and "/upload" in html and 'id="usage"' in html
    # every answer ends with the documents it was read from, linked to the console's viewer
    assert "Evidence — " in html and "/doc?path=" in html and "Scoreboard" in html
    assert 'data-prices=""' in html
    monkeypatch.setenv("FREIGHT_PRICE_IN_PER_M", "0.30")
    monkeypatch.setenv("FREIGHT_PRICE_OUT_PER_M", "2.50")
    assert 'data-prices="[0.3, 2.5]"' in chatui.page()


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
