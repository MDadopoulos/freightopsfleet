"""Fleet CLI — chat, catalog, approvals, ledger.

    python -m freight_fleet.cli chat --session SID "message"
    python -m freight_fleet.cli sweep [--date TAG]
    python -m freight_fleet.cli catalog
    python -m freight_fleet.cli ledger [--session SID]
    python -m freight_fleet.cli approvals list
    python -m freight_fleet.cli approvals grant <approval_id>
    python -m freight_fleet.cli console [--port 8080]

`chat` runs ONE turn against the coordinator on a DURABLE session
(DatabaseSessionService over SQLite): each invocation is its own process, so
"kill the process, restart it, resume the session" is not a demo trick here —
it is simply how the command works. The same session id in a later invocation
continues the same conversation with its history. Vertex AI sessions are the
deploy-time swap; the session id and app name do not change.

`catalog` and `ledger` work today with no credentials — they read code-owned data
and the JSONL ledger. `approvals` shares the durable `FileApprovalStore` with the
fleet process, so a hold placed by the sweep is grantable from a later shell.

`console` serves the same four artifacts — ledger, approval store, catalog,
committed runs — as HTML for an operator who does not live in a terminal. It
never calls a model, and its approve button goes through
`governance.gate.execute_approved`, the identical function `approvals grant`
calls. Two front doors, one seam.
"""

from __future__ import annotations

import argparse
import os

from .catalog.registry import catalog
from .governance.ledger import Ledger

_LEDGER_PATH = os.environ.get("FREIGHT_LEDGER_PATH", "audit/ledger.jsonl")
_SESSIONS_DB = os.environ.get("FREIGHT_SESSIONS_DB", "sqlite+aiosqlite:///./data/sessions.db")
_APPROVALS_PATH = os.environ.get("FREIGHT_APPROVALS_PATH", "data/approvals.json")
_APP = "freight_fleet"
_USER = "operator"


def _approval_store():
    from .governance.gate import FileApprovalStore

    return FileApprovalStore(_APPROVALS_PATH)


def cmd_chat(args: argparse.Namespace) -> int:
    import asyncio
    from pathlib import Path

    from google.adk.runners import Runner
    from google.adk.sessions import DatabaseSessionService
    from google.genai import types

    from .agents.fleet import build_fleet

    if ":///" in _SESSIONS_DB and _SESSIONS_DB.startswith("sqlite"):
        Path(_SESSIONS_DB.split(":///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)

    async def turn() -> int:
        session_service = DatabaseSessionService(db_url=_SESSIONS_DB)
        session = await session_service.get_session(
            app_name=_APP, user_id=_USER, session_id=args.session
        )
        resumed = session is not None
        if not resumed:
            await session_service.create_session(
                app_name=_APP, user_id=_USER, session_id=args.session
            )
        coordinator, _, _ = build_fleet(
            model=args.model, ledger=Ledger(_LEDGER_PATH),
            approvals=_approval_store(), session_id=args.session,
        )
        runner = Runner(agent=coordinator, app_name=_APP, session_service=session_service)
        print(f"\n  session {args.session} "
              f"({'resumed, ' + str(len(session.events)) + ' prior events' if resumed else 'new'})\n")
        final = ""
        async for event in runner.run_async(
            user_id=_USER,
            session_id=args.session,
            new_message=types.Content(role="user", parts=[types.Part(text=args.message)]),
        ):
            if event.is_final_response() and event.content and event.content.parts:
                final = "".join(p.text or "" for p in event.content.parts)
        print(final)
        return 0

    return asyncio.run(turn())


def cmd_sweep(args: argparse.Namespace) -> int:
    """The unattended morning run (BUILD-PLAN step 6b): cross-check every open
    shipment, hold anything consequential, touch nothing. Cloud Scheduler at
    deploy time simply cron-invokes this command - the honesty of the async
    story is that nobody needs to be watching when it runs."""
    import asyncio
    from pathlib import Path

    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from .agents.fleet import _specialist
    from .catalog.registry import get_card
    from .governance.gate import make_before_tool_gate

    workspace = Path(os.environ.get("FREIGHT_WORKSPACE_ROOT", "./workspace"))
    shipments = sorted(p.name for p in (workspace / "shipments").iterdir() if p.is_dir())
    if not shipments:
        print("  no shipments in the workspace - seed it first")
        return 1

    session_id = f"sweep-{args.date}" if args.date else "sweep"
    ledger = Ledger(_LEDGER_PATH)
    store = _approval_store()
    gate = make_before_tool_gate(ledger, store, session_id=session_id)
    card = get_card("cross_check")

    async def one(shipment: str) -> str:
        agent = _specialist(card, args.model, gate)
        session_service = InMemorySessionService()
        sid = f"{session_id}-{shipment}"
        await session_service.create_session(app_name=_APP, user_id=_USER, session_id=sid)
        runner = Runner(agent=agent, app_name=_APP, session_service=session_service)
        prompt = (
            f"Unattended morning sweep. Cross-check shipments/{shipment}. "
            "If there are discrepancies, draft the notice and save it under outbox/ "
            "(it will be held for approval). Finish with one line: "
            f"'{shipment}: N discrepancies'."
        )
        final = ""
        async for event in runner.run_async(
            user_id=_USER, session_id=sid,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        ):
            if event.is_final_response() and event.content and event.content.parts:
                final = "".join(p.text or "" for p in event.content.parts)
        return final

    async def run_all() -> None:
        print(f"\n  SWEEP {session_id} - {len(shipments)} open shipment(s)\n")
        for shipment in shipments:
            final = await one(shipment)
            last = next((ln.strip() for ln in reversed(final.splitlines()) if ln.strip()), "")
            print(f"  {shipment:24} {last[:120]}")
        pending = store.pending()
        print(f"\n  {len(pending)} draft(s) held for approval; nothing sent, nothing written.")
        for aid, payload in pending.items():
            print(f"    {aid}  {payload.get('args', {}).get('path', '?')}")

    asyncio.run(run_all())
    return 0


def cmd_catalog(_args: argparse.Namespace) -> int:
    rows = catalog()
    print(f"\n  FLEET CATALOG — {len(rows)} agents\n")
    for c in rows:
        print(f"  {c['name']}")
        print(f"    key        {c['key']}")
        print(f"    desk       {c['desk']}  (owner: {c['owner']})")
        print(f"    autonomy   {c['autonomy']}")
        print(f"    data scope {c['data_scope']}")
        print(f"    tools      {', '.join(c['tools'])}")
        print(f"    cap        ${c['max_usd_per_run']:.2f}/run\n")
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    entries = list(Ledger(_LEDGER_PATH).read())
    if args.session:
        entries = [e for e in entries if e.session_id == args.session]
    if not entries:
        print(f"\n  no ledger entries at {_LEDGER_PATH}\n")
        return 0
    print(f"\n  AUDIT LEDGER — {len(entries)} decisions\n")
    print(f"  {'time':20} {'agent':18} {'tool':12} {'verdict':8} {'outcome':10} detail")
    for e in entries:
        print(f"  {e.ts:20} {e.agent[:18]:18} {e.tool[:12]:12} "
              f"{e.verdict:8} {e.outcome:10} {e.detail[:40]}")
    # A hold is resolved once a later row carries its id as approved/rejected —
    # otherwise an approved action keeps showing as pending forever.
    resolved = {e.approval_id for e in entries if e.outcome in {"approved", "rejected"}}
    held = [e for e in entries if e.outcome == "held" and e.entry_id not in resolved]
    if held:
        print(f"\n  {len(held)} action(s) awaiting approval:")
        for e in held:
            print(f"    {e.entry_id}  {e.tool}  {e.args_digest}")
    print()
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    """List, grant or reject. Grant and reject are thin printers over
    `governance.gate` — the replay lives at the seam, not in the front door, so
    the console cannot drift from the CLI about what approving means."""
    from .agents.fleet import _TOOL_FNS
    from .governance.gate import execute_approved, reject_approved

    store = _approval_store()

    if args.action == "list":
        pending = store.pending()
        if not pending:
            print("\n  no actions awaiting approval\n")
            return 0
        print(f"\n  {len(pending)} action(s) awaiting approval\n")
        for aid, payload in pending.items():
            print(f"  {aid}")
            print(f"    agent {payload.get('agent')}  tool {payload.get('tool')}")
            args_ = payload.get("args") or {}
            print(f"    path  {args_.get('path', '?')}")
            if "content" in args_:
                preview = str(args_["content"])[:400].rstrip()
                print("    --- draft ---")
                for line in preview.splitlines():
                    print(f"    | {line}")
                print("    --- end ---")
        print()
        return 0

    if not args.approval_id:
        print("  approval id required")
        return 1
    aid = args.approval_id

    if args.action == "reject":
        res = reject_approved(aid, ledger=Ledger(_LEDGER_PATH), approvals=store,
                              source="approval-cli")
        if res.status == "not_pending":
            print(f"  no pending approval {aid}")
            return 1
        print(f"  rejected {aid} ({res.tool} {res.path})")
        return 0

    # grant: mark granted, then REPLAY THROUGH THE GATE - the seam stays single.
    res = execute_approved(aid, ledger=Ledger(_LEDGER_PATH), approvals=store,
                           tool_fns=_TOOL_FNS, source="approval-cli")
    if res.status == "not_pending":
        print(f"  no pending approval {aid}")
        return 1
    if res.status == "not_executable":
        print(f"  {res.tool} has no executable body wired (by design for send_email)")
        return 1
    if res.status == "gate_refused":
        print(f"  gate refused the replay: {(res.gate or {}).get('status')} - not executing")
        return 1
    print(f"  executed {res.tool} -> {res.path} ({res.bytes} bytes, {res.detail})")
    return 0


def cmd_console(args: argparse.Namespace) -> int:
    """Serve the operator console. No credentials, no model — every page is a
    read of the ledger, the approval store, the catalog or a committed run."""
    import uvicorn

    print(f"\n  operator console on http://{args.host}:{args.port}  (no credentials needed)\n")
    uvicorn.run("freight_fleet.console:app", host=args.host, port=args.port, log_level="info")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="freight_fleet.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_chat = sub.add_parser("chat", help="one turn against the coordinator on a durable session")
    p_chat.add_argument("message")
    p_chat.add_argument("--session", default="local")
    p_chat.add_argument("--model", default=os.environ.get("FREIGHT_MODEL", "gemini-3.7-flash"))
    p_chat.set_defaults(fn=cmd_chat)

    p_sweep = sub.add_parser("sweep", help="unattended cross-check of every open shipment")
    p_sweep.add_argument("--date", default="", help="tag for the sweep session id")
    p_sweep.add_argument("--model", default=os.environ.get("FREIGHT_MODEL", "gemini-3.7-flash"))
    p_sweep.set_defaults(fn=cmd_sweep)

    sub.add_parser("catalog", help="print the fleet catalog").set_defaults(fn=cmd_catalog)

    p_ledger = sub.add_parser("ledger", help="print the audit ledger")
    p_ledger.add_argument("--session")
    p_ledger.set_defaults(fn=cmd_ledger)

    p_appr = sub.add_parser("approvals", help="list or grant pending approvals")
    p_appr.add_argument("action", choices=["list", "grant", "reject"])
    p_appr.add_argument("approval_id", nargs="?")
    p_appr.set_defaults(fn=cmd_approvals)

    p_console = sub.add_parser("console", help="serve the operator console (no credentials)")
    p_console.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    p_console.add_argument("--host", default="127.0.0.1")
    p_console.set_defaults(fn=cmd_console)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
