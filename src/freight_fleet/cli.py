"""Fleet CLI — chat, catalog, approvals, ledger.

    python -m freight_fleet.cli chat --session SID "message"
    python -m freight_fleet.cli sweep [--date TAG]
    python -m freight_fleet.cli catalog
    python -m freight_fleet.cli ledger [--session SID]
    python -m freight_fleet.cli approvals list
    python -m freight_fleet.cli approvals grant <approval_id>

`chat` runs ONE turn against the coordinator on a DURABLE session
(DatabaseSessionService over SQLite): each invocation is its own process, so
"kill the process, restart it, resume the session" is not a demo trick here —
it is simply how the command works. The same session id in a later invocation
continues the same conversation with its history. Vertex AI sessions are the
deploy-time swap; the session id and app name do not change.

`catalog` and `ledger` work today with no credentials — they read code-owned data
and the JSONL ledger. `approvals` needs the running fleet process to share an
ApprovalStore, so wire it to your server in BUILD-PLAN step 7; the read side
(listing what the ledger says is held) works standalone right now.
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
    from .agents.fleet import _TOOL_FNS
    from .governance.gate import make_before_tool_gate

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
        payload = store.reject(aid)
        if payload is None:
            print(f"  no pending approval {aid}")
            return 1
        Ledger(_LEDGER_PATH).append(_decision_row(payload, aid, "rejected", "operator rejected"))
        print(f"  rejected {aid} ({payload.get('tool')} {payload.get('args', {}).get('path', '')})")
        return 0

    # grant: mark granted, then REPLAY THROUGH THE GATE - the seam stays single.
    payload = store.approve(aid)
    if payload is None:
        print(f"  no pending approval {aid}")
        return 1
    name = payload["tool"]
    fn = _TOOL_FNS.get(name)
    if fn is None:
        print(f"  {name} has no executable body wired (by design for send_email)")
        return 1
    ledger = Ledger(_LEDGER_PATH)
    gate = make_before_tool_gate(ledger, store, session_id="approval-cli")
    replay_args = dict(payload.get("args") or {})
    replay_args["_approval_id"] = aid

    class _ReplayTool:
        pass

    tool = _ReplayTool()
    tool.name = name

    class _ReplayCtx:
        agent_name = payload.get("agent", "operator")

    verdict = gate(tool, replay_args, _ReplayCtx())
    if verdict is not None:
        print(f"  gate refused the replay: {verdict.get('status')} - not executing")
        return 1
    replay_args.pop("_approval_id", None)
    result = fn(**replay_args)
    ledger.append(_decision_row(payload, aid, "executed",
                                f"replayed after approval; result status={result.get('status')}"))
    print(f"  executed {name} -> {result}")
    return 0


def _decision_row(payload: dict, approval_id: str, outcome: str, detail: str):
    from .governance.gate import digest_args
    from .governance.ledger import LedgerEntry

    return LedgerEntry.new(
        session_id="approval-cli", agent="operator", tool=payload.get("tool", "?"),
        risk="high", verdict="ask", outcome=outcome,
        args_digest=digest_args(payload.get("args") or {}),
        approval_id=approval_id, detail=detail,
    )


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

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
