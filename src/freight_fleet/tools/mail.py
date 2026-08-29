"""`send_email` — the one tool whose mistake cannot be undone, so the one whose
body is the most constrained.

Three facts decide this module's shape:

1. **The model never chooses where mail goes.** The `to` argument is what the
   draft *says* the recipient is — a carrier, a shipper's agent, a customer —
   and it is recorded as the *intended* recipient. Actual delivery is to the
   operator-configured sink (`FREIGHT_MAIL_SINK`) and, when the approving human
   signed in with an email address, to that human. A chat box that could email
   an arbitrary address after one click would be a spam relay with a nice UI.
2. **Every send is a human's click.** `send_email` is CRITICAL with an external
   side effect in `governance.policy`, so the gate holds it on every path and
   this body only ever runs as an approved replay. There is no code path from a
   model turn to `smtplib`.
3. **The spool is the evidence.** Whatever the transport, every delivered
   message is also written as one JSON file under `FREIGHT_MAIL_SPOOL`, which is
   what the console's Sent page reads. The ledger row says *that* a send ran;
   the spool says *what* left, to whom, approved by whom.

Transports (`FREIGHT_MAIL_TRANSPORT`):

* `spool` (default) — deliver to the spool only. The demo mailbox is the Sent
  page. Honest and free; it is what a deployment runs until somebody hands it
  SMTP credentials.
* `smtp` — STARTTLS to `FREIGHT_SMTP_HOST:FREIGHT_SMTP_PORT` as
  `FREIGHT_SMTP_USER` / `FREIGHT_SMTP_PASSWORD` (a Gmail app password, from
  Secret Manager, never an env literal in a deploy flag), then spool. A missing
  sink or password is an error *result*, not a silent downgrade to `spool`: a
  send the operator believed went out and did not is the worst outcome here.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import smtplib
import uuid
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

#: Who approved the send, set by the approval surface around the replay so the
#: message can carry a copy to them. A contextvar rather than a tool argument
#: because the tool's signature is the model's contract, and "who approved this"
#: is not something the model gets to fill in.
APPROVER: contextvars.ContextVar[str] = contextvars.ContextVar("freight_mail_approver", default="")

#: Subject prefix on everything that leaves. A judge's inbox must not confuse a
#: demo notice with a real one, and a real carrier must never receive one.
SUBJECT_PREFIX = "[Freight Ops demo]"

_EMAIL_RX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def spool_dir() -> Path:
    return Path(os.environ.get("FREIGHT_MAIL_SPOOL", "data/sent"))


def transport() -> str:
    return os.environ.get("FREIGHT_MAIL_TRANSPORT", "spool").strip().lower() or "spool"


def sink() -> str:
    return os.environ.get("FREIGHT_MAIL_SINK", "").strip()


def recipients(intended: str, approver: str = "") -> list[str]:
    """Where a message actually goes: the sink, plus the approver when the
    approver is an address. Never `intended` — see the module docstring."""
    out: list[str] = []
    for addr in (sink(), approver.strip()):
        if addr and _EMAIL_RX.match(addr) and addr.lower() not in {o.lower() for o in out}:
            out.append(addr)
    return out


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _record(msg_id: str, *, to: str, subject: str, body: str, delivered: list[str],
            approver: str, via: str) -> Path:
    path = spool_dir() / f"{_now().replace(':', '')}-{msg_id[:8]}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": msg_id,
        "ts": _now(),
        "intended_to": to,
        "delivered_to": delivered,
        "subject": subject,
        "body": body,
        "transport": via,
        "approved_by": approver,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def _smtp_send(msg_id: str, *, to: str, subject: str, body: str, delivered: list[str]) -> None:
    host = os.environ.get("FREIGHT_SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("FREIGHT_SMTP_PORT", "587"))
    user = os.environ.get("FREIGHT_SMTP_USER", "").strip()
    password = os.environ.get("FREIGHT_SMTP_PASSWORD", "")
    sender = os.environ.get("FREIGHT_MAIL_FROM", "").strip() or user
    if not (user and password and sender):
        raise RuntimeError("smtp transport selected but FREIGHT_SMTP_USER/FREIGHT_SMTP_PASSWORD are unset")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(delivered)
    message["Subject"] = f"{SUBJECT_PREFIX} {subject}".strip()
    message["X-Freight-Demo-Intended-To"] = to
    message["X-Freight-Demo-Message-Id"] = msg_id
    message.set_content(
        f"Intended recipient (as drafted by the fleet, NOT delivered there): {to}\n"
        f"Delivered to: {', '.join(delivered)}\n"
        "This message left a governed demo after a human approved it. "
        "Every party and shipment in it is fictional.\n\n"
        "----------------------------------------------------------------\n\n"
        f"{body}"
    )
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(message)


def send_email(to: str, subject: str, body: str) -> dict:
    """Send a drafted notice. CRITICAL and external — always held by the gate.

    The message is delivered to the configured demo mailbox (and to the human
    who approved it, if they signed in with an email address), never to `to`
    itself; `to` is recorded as the intended recipient.

    Args:
        to: The intended recipient as the documents name them, e.g. "ops@carrier.example".
        subject: The subject line, references first, e.g. "[BK4471 / MERU26071234] Discrepancy notice".
        body: The full message body, plain text or markdown.
    """
    to = (to or "").strip()
    subject = (subject or "").strip()
    if not to or not subject or not (body or "").strip():
        return {"status": "error", "message": "send_email needs a recipient, a subject and a body"}
    approver = APPROVER.get()
    delivered = recipients(to, approver)
    via = transport()
    msg_id = str(uuid.uuid4())
    if via == "smtp":
        if not delivered:
            return {"status": "error",
                    "message": "smtp transport has nowhere to deliver: FREIGHT_MAIL_SINK is unset"}
        try:
            _smtp_send(msg_id, to=to, subject=subject, body=body, delivered=delivered)
        except Exception as exc:  # noqa: BLE001 - the ledger must record the failure, not a traceback
            return {"status": "error", "message": f"{type(exc).__name__}: {str(exc)[:200]}"}
    elif via != "spool":
        return {"status": "error", "message": f"unknown mail transport {via!r}"}
    record = _record(msg_id, to=to, subject=subject, body=body, delivered=delivered,
                     approver=approver, via=via)
    return {
        "status": "ok",
        "message_id": msg_id,
        "intended_to": to,
        "delivered_to": delivered,
        "transport": via,
        "spool": record.name,
    }


def list_sent() -> list[dict[str, Any]]:
    """Every spooled message, newest first. Malformed files are reported as
    rows rather than skipped — the Sent page is evidence, and evidence with a
    hole in it must show the hole."""
    root = spool_dir()
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError("not an object")
            data["_file"] = path.name
            rows.append(data)
        except (OSError, ValueError, TypeError) as exc:
            rows.append({"_file": path.name, "_error": f"{type(exc).__name__}: {exc}"})
    return rows


#: Tool name -> body, merged with `workspace.TOOL_FNS` by the ADK assembly and
#: by every approval surface. Kept apart from the workspace map because the
#: jail and the mail are different kinds of thing, and a reader of either file
#: should see only the one.
TOOL_FNS = {"send_email": send_email}
