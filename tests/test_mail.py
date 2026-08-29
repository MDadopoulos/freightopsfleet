"""`send_email`: the recipient policy, the spool, the two transports."""

from __future__ import annotations

import json

import pytest

from freight_fleet.tools import mail


@pytest.fixture()
def spool(tmp_path, monkeypatch):
    monkeypatch.setenv("FREIGHT_MAIL_SPOOL", str(tmp_path / "sent"))
    monkeypatch.delenv("FREIGHT_MAIL_TRANSPORT", raising=False)
    monkeypatch.delenv("FREIGHT_MAIL_SINK", raising=False)
    for var in ("FREIGHT_SMTP_USER", "FREIGHT_SMTP_PASSWORD", "FREIGHT_MAIL_FROM"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "sent"


def test_the_drafted_address_is_recorded_and_never_delivered_to(spool, monkeypatch):
    monkeypatch.setenv("FREIGHT_MAIL_SINK", "demo@example.test")
    token = mail.APPROVER.set("judge@gmail.test")
    try:
        out = mail.send_email("ops@carrier.example", "[BK1] Notice", "body")
    finally:
        mail.APPROVER.reset(token)
    assert out["status"] == "ok" and out["transport"] == "spool"
    assert out["intended_to"] == "ops@carrier.example"
    assert out["delivered_to"] == ["demo@example.test", "judge@gmail.test"]
    rows = mail.list_sent()
    assert len(rows) == 1 and rows[0]["approved_by"] == "judge@gmail.test"
    assert "ops@carrier.example" not in rows[0]["delivered_to"]
    assert json.loads((spool / rows[0]["_file"]).read_text(encoding="utf-8"))["subject"] == "[BK1] Notice"


def test_a_bare_username_is_named_but_not_mailed(spool):
    token = mail.APPROVER.set("judge1")
    try:
        out = mail.send_email("x@y.test", "s", "b")
    finally:
        mail.APPROVER.reset(token)
    assert out["delivered_to"] == []
    assert mail.list_sent()[0]["approved_by"] == "judge1"


def test_recipients_dedupe_and_ignore_nonsense(monkeypatch):
    monkeypatch.setenv("FREIGHT_MAIL_SINK", "Demo@Example.test")
    assert mail.recipients("anyone@x.test", "demo@example.test") == ["Demo@Example.test"]
    assert mail.recipients("anyone@x.test", "not an address") == ["Demo@Example.test"]
    monkeypatch.setenv("FREIGHT_MAIL_SINK", "garbage")
    assert mail.recipients("anyone@x.test") == []


def test_missing_fields_are_an_error_result_not_a_send(spool):
    assert mail.send_email("", "s", "b")["status"] == "error"
    assert mail.send_email("a@b.test", "", "b")["status"] == "error"
    assert mail.send_email("a@b.test", "s", "  ")["status"] == "error"
    assert mail.list_sent() == []


def test_smtp_transport_sends_to_the_sink_with_the_demo_headers(spool, monkeypatch):
    monkeypatch.setenv("FREIGHT_MAIL_TRANSPORT", "smtp")
    monkeypatch.setenv("FREIGHT_MAIL_SINK", "demo@example.test")
    monkeypatch.setenv("FREIGHT_SMTP_USER", "demo@example.test")
    monkeypatch.setenv("FREIGHT_SMTP_PASSWORD", "app-password")
    sent: list = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self):
            sent.append(("starttls",))

        def login(self, user, password):
            sent.append(("login", user))

        def send_message(self, message):
            sent.append(("message", message))

    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    out = mail.send_email("ops@carrier.example", "[BK1] Notice", "the body")
    assert out["status"] == "ok" and out["transport"] == "smtp"
    assert sent[0] == ("connect", "smtp.gmail.com", 587) and ("starttls",) in sent
    msg = sent[-1][1]
    assert msg["To"] == "demo@example.test"
    assert msg["Subject"] == "[Freight Ops demo] [BK1] Notice"
    assert msg["X-Freight-Demo-Intended-To"] == "ops@carrier.example"
    assert "NOT delivered there" in msg.get_content() and "the body" in msg.get_content()
    assert mail.list_sent()[0]["transport"] == "smtp"


def test_smtp_without_a_sink_or_credentials_fails_closed(spool, monkeypatch):
    monkeypatch.setenv("FREIGHT_MAIL_TRANSPORT", "smtp")
    out = mail.send_email("a@b.test", "s", "b")
    assert out["status"] == "error" and "FREIGHT_MAIL_SINK" in out["message"]
    monkeypatch.setenv("FREIGHT_MAIL_SINK", "demo@example.test")
    out = mail.send_email("a@b.test", "s", "b")
    assert out["status"] == "error" and "FREIGHT_SMTP_USER" in out["message"]
    assert mail.list_sent() == [], "a failed send is not spooled as if it left"


def test_an_unreadable_spool_file_is_a_row_not_a_gap(spool):
    spool.mkdir()
    (spool / "bad.json").write_text("{not json", encoding="utf-8")
    rows = mail.list_sent()
    assert len(rows) == 1 and "_error" in rows[0] and rows[0]["_file"] == "bad.json"
