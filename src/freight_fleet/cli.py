"""Fleet CLI — chat, catalog, approvals, ledger.

    python -m freight_fleet.cli chat --session SID "message"
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
_APP = "freight_fleet"
_USER = "operator"


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
            model=args.model, ledger=Ledger(_LEDGER_PATH), session_id=args.session
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
    if args.action == "list":
        return cmd_ledger(argparse.Namespace(session=None))
    print("\n  Granting requires the running fleet process (BUILD-PLAN step 7).\n"
          "  Wire this to the server that owns the ApprovalStore.\n")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="freight_fleet.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_chat = sub.add_parser("chat", help="one turn against the coordinator on a durable session")
    p_chat.add_argument("message")
    p_chat.add_argument("--session", default="local")
    p_chat.add_argument("--model", default=os.environ.get("FREIGHT_MODEL", "gemini-3.7-flash"))
    p_chat.set_defaults(fn=cmd_chat)

    sub.add_parser("catalog", help="print the fleet catalog").set_defaults(fn=cmd_catalog)

    p_ledger = sub.add_parser("ledger", help="print the audit ledger")
    p_ledger.add_argument("--session")
    p_ledger.set_defaults(fn=cmd_ledger)

    p_appr = sub.add_parser("approvals", help="list or grant pending approvals")
    p_appr.add_argument("action", choices=["list", "grant"])
    p_appr.add_argument("approval_id", nargs="?")
    p_appr.set_defaults(fn=cmd_approvals)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
