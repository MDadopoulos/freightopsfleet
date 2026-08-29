"""Fleet CLI — chat, catalog, approvals, ledger.

    python -m freight_fleet.cli chat --session SID "message"
    python -m freight_fleet.cli sweep [--date TAG]
    python -m freight_fleet.cli catalog
    python -m freight_fleet.cli ledger [--session SID]
    python -m freight_fleet.cli approvals list
    python -m freight_fleet.cli approvals grant <approval_id>
    python -m freight_fleet.cli approvals reconcile
    python -m freight_fleet.cli approvals abandon <approval_id> [--note "..."]
    python -m freight_fleet.cli ingest [--only GLOB] [--force] [--dry-run]
    python -m freight_fleet.cli console [--port 8080]

`chat` runs ONE turn against the coordinator on a DURABLE session
(DatabaseSessionService over SQLite): each invocation is its own process, so
"kill the process, restart it, resume the session" is not a demo trick here —
it is simply how the command works. The same session id in a later invocation
continues the same conversation with its history. Vertex AI sessions are the
deploy-time swap; the session id and app name do not change.

`sweep` is durable too, and on a different axis: its LEDGER session id is the
run (`sweep-<date>`), its ADK conversation id is the SUBJECT
(`shipment-<dir>`). The first changes every morning, the second must not — a
scheduled sweep that rebuilt its sessions each run would be the half of the
entry that forgets. See `cmd_sweep`.

`approvals reconcile` compares the two: the ledger is the authority for what
happened, the approval store for what is actionable, and they are two files
written by two unsynchronised writes. It exits 1 when they disagree, so it works
as a healthcheck. `approvals list` reports the same disagreement inline, because
"no actions awaiting approval" printed while the ledger holds six unresolved
rows is a false statement on the operator's decision surface.

`catalog` and `ledger` work today with no credentials — they read code-owned data
and the JSONL ledger. `approvals` shares the durable `FileApprovalStore` with the
fleet process, so a hold placed by the sweep is grantable from a later shell.

`ingest` is the one command here that reads the ARRIVAL surface: it transcribes
the PDFs and scans under `workspace/raw/` into markdown under `workspace/inbox/`,
which is where the fleet already looks. It is a command and not a tool on
purpose — see `freight_fleet.ingest` for why. `--dry-run` prints the plan and
needs no credentials.

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


def sweep_session_id(shipment: str) -> str:
    """The ADK conversation id for one shipment. Module-level so it is testable,
    and so the fact that it takes NO date is visible from the signature: a
    parameter it does not have is a way it cannot break."""
    return f"shipment-{shipment}"


def cmd_sweep(args: argparse.Namespace) -> int:
    """The unattended morning run (BUILD-PLAN step 6b): cross-check every open
    shipment, hold anything consequential, touch nothing. Cloud Scheduler at
    deploy time simply cron-invokes this command - the honesty of the async
    story is that nobody needs to be watching when it runs.

    TWO SESSION IDS, TWO DIFFERENT QUESTIONS. They are not the same axis and
    collapsing them is what broke continuity here:

    * The LEDGER session id is `sweep-<date>` — the identity of THIS RUN. It
      answers "what did the sweep of 21 Aug decide?", and the console groups the
      record by it. It must change every morning.
    * The ADK conversation id is `shipment-<dir>` — the identity of the SUBJECT.
      It answers "what does the fleet already know about this shipment?", and it
      must NOT change every morning, or the unattended half of the entry forgets
      everything overnight while the attended half (`chat`) remembers.

    Before this, the sweep built an `InMemorySessionService` per shipment per
    run: a sweep that ran every morning for three weeks started from nothing
    twenty-one times. Now it uses `DatabaseSessionService` over the same SQLite
    file `chat` uses, so consecutive sweeps of one shipment continue ONE
    conversation, and killing the process between mornings changes nothing.

    Why the shipment DIRECTORY NAME is the identity: it is the workspace's own
    name for the shipment, it is already the unit this loop iterates and the unit
    D1 derives on the console, and it is stable across runs without inventing a
    key. Per shipment rather than one session for the whole sweep, because a
    single shared conversation would carry one shipment's documents and findings
    into the next shipment's context — that is a data-scope leak, not merely
    untidiness. If a shipment folder is renamed its history legitimately starts
    over: a renamed folder is a different subject as far as this fleet can tell,
    and mapping it onto the old history would be inventing continuity.
    """
    import asyncio
    from pathlib import Path

    from google.adk.runners import Runner
    from google.adk.sessions import DatabaseSessionService
    from google.genai import types

    from .agents.fleet import _specialist
    from .catalog.registry import get_card
    from .governance.gate import make_before_tool_gate, open_store, reconcile

    workspace = Path(os.environ.get("FREIGHT_WORKSPACE_ROOT", "./workspace"))
    shipments = sorted(p.name for p in (workspace / "shipments").iterdir() if p.is_dir())
    if not shipments:
        print("  no shipments in the workspace - seed it first")
        return 1

    # Same URL requirement as `chat`: DatabaseSessionService needs the async
    # driver, and SQLite will not create a missing parent directory for itself.
    if ":///" in _SESSIONS_DB and _SESSIONS_DB.startswith("sqlite"):
        Path(_SESSIONS_DB.split(":///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)

    run_id = f"sweep-{args.date}" if args.date else "sweep"
    ledger = Ledger(_LEDGER_PATH)
    store = _approval_store()
    gate = make_before_tool_gate(ledger, store, session_id=run_id)
    card = get_card("cross_check")

    async def one(session_service, shipment: str) -> tuple[str, int]:
        agent = _specialist(card, args.model, gate)
        sid = sweep_session_id(shipment)
        session = await session_service.get_session(
            app_name=_APP, user_id=_USER, session_id=sid
        )
        if session is None:
            await session_service.create_session(
                app_name=_APP, user_id=_USER, session_id=sid
            )
            prior = 0
        else:
            prior = len(session.events)

        # The prompt states only what the session service can prove: how many
        # events this conversation already carries. It never asserts a number of
        # previous sweeps or a span of weeks - a first run on a fresh database
        # must not be told it has history it does not have.
        if prior:
            history = (
                f"You have checked this shipment before: this conversation already carries "
                f"{prior} event(s), and your earlier findings are above in it. Re-check the "
                "documents as they stand now. Say plainly whether what you find MATCHES your "
                "last check or DIFFERS from it, and name what changed if anything did. Do not "
                "repeat the full report when nothing has moved."
            )
        else:
            history = (
                "This is the first check of this shipment on this conversation, so there is "
                "nothing to compare against yet. State your findings plainly enough that the "
                "next sweep can compare against them."
            )
        prompt = (
            f"Unattended morning sweep ({run_id}). Cross-check shipments/{shipment}. "
            f"{history} "
            "If there are discrepancies, draft the notice and send it with send_email to the "
            "party the documents name as responsible (it will be held for a human; do not "
            "also save it). Finish with one line: "
            f"'{shipment}: N discrepancies'."
        )
        runner = Runner(agent=agent, app_name=_APP, session_service=session_service)
        final = ""
        async for event in runner.run_async(
            user_id=_USER, session_id=sid,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        ):
            if event.is_final_response() and event.content and event.content.parts:
                final = "".join(p.text or "" for p in event.content.parts)
        return final, prior

    async def run_all() -> list[str]:
        session_service = DatabaseSessionService(db_url=_SESSIONS_DB)
        print(f"\n  SWEEP {run_id} - {len(shipments)} open shipment(s)")
        print(f"  conversations are durable: {_SESSIONS_DB}\n")
        failed: list[str] = []
        for shipment in shipments:
            # One shipment's transient model error must not end the sweep. This
            # is the UNATTENDED path -- nobody is watching, and the tail of this
            # function is where the operator's desk gets summarized and where the
            # stranding self-check runs. Letting a 502 propagate skipped every
            # remaining shipment AND both of those, so a morning that held two
            # drafts reported a traceback instead of a queue. Observed live: a
            # `502 policy context unavailable` on shipment 4 of 6.
            try:
                final, prior = await one(session_service, shipment)
            except Exception as exc:  # noqa: BLE001 - one bad shipment must not eat the run
                failed.append(shipment)
                print(f"  {shipment:24} [FAILED] {type(exc).__name__}: {str(exc)[:80]}")
                continue
            last = next((ln.strip() for ln in reversed(final.splitlines()) if ln.strip()), "")
            cont = f"resumed, {prior} prior events" if prior else "new conversation"
            print(f"  {shipment:24} [{cont}] {last[:100]}")
        if failed:
            print(f"\n  !! {len(failed)} of {len(shipments)} shipment(s) were NOT checked: "
                  f"{', '.join(failed)}")
            print("     Their conversations are unchanged; the next sweep resumes them.")
        pending = store.pending()
        print(f"\n  {len(pending)} draft(s) held for approval; nothing sent, nothing written.")
        for aid, payload in pending.items():
            a = payload.get('args', {})
            print(f"    {aid}  {a.get('path') or a.get('subject') or '?'}")
        # The sweep is exactly where a hold gets stranded, so it checks its own
        # work before it exits rather than leaving the operator to find out from
        # an empty queue tomorrow morning.
        recon = reconcile(Ledger(_LEDGER_PATH), open_store(_APPROVALS_PATH))
        if recon.diverged:
            print(f"\n  WARNING: the record and the approval store disagree "
                  f"({len(recon.stranded)} stranded, {len(recon.orphaned)} orphaned). "
                  f"Run: python -m freight_fleet.cli approvals reconcile")
        return failed

    # A sweep that silently skipped shipments is not a successful sweep: the
    # scheduler invoking this must be able to tell the difference.
    return 1 if asyncio.run(run_all()) else 0


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
    from .governance.gate import RESOLVING_OUTCOMES

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
    # A hold is resolved once a later row carries its id as one of
    # RESOLVING_OUTCOMES — imported, not restated, because this rule and the
    # console's copy of it had already drifted apart.
    resolved = {e.approval_id for e in entries if e.outcome in RESOLVING_OUTCOMES}
    held = [e for e in entries if e.outcome == "held" and e.entry_id not in resolved]
    if held:
        print(f"\n  {len(held)} action(s) awaiting approval:")
        for e in held:
            print(f"    {e.entry_id}  {e.tool}  {e.args_digest}")
    print()
    return 0


def _stranded_block(recon, limit: int = 5) -> str:
    """The warning `approvals list` must print instead of a clear desk.

    Rendered from the LEDGER row only. There is no draft body here and no way to
    ask for one: the store held it, `digest_args` kept its length, and a
    placeholder of the same length fingerprints identically to the original — so
    saying "NOT RECOVERABLE" out loud is the only honest line available.
    """
    if not recon.diverged:
        return ""
    out: list[str] = [""]
    if not recon.store_readable:
        out.append("  !! THE APPROVAL STORE COULD NOT BE READ.")
        out.append("     This is not an empty queue. Nothing below can be approved until the")
        out.append(f"     file at {_APPROVALS_PATH} is readable again.")
    if recon.stranded:
        out.append(f"  !! {len(recon.stranded)} held action(s) are in the LEDGER but NOT in the")
        out.append("     approval store. They cannot be approved and nothing will run them.")
        out.append("")
        for st in recon.stranded[:limit]:
            out.append(f"     {st.approval_id}  {st.tool}  {st.path or '?'}")
            out.append(f"         held {st.ts} by {st.agent} in {st.session_id}")
            chars = "unknown" if st.content_chars is None else f"{st.content_chars} characters"
            out.append(f"         draft body NOT RECOVERABLE — {chars} recorded, "
                       "the content was never in the ledger")
        if len(recon.stranded) > limit:
            out.append(f"     ... {len(recon.stranded) - limit} more")
    if recon.orphaned:
        out.append(f"  !! {len(recon.orphaned)} store entr(ies) have NO held row in the ledger.")
        out.append("     These fail OPEN: approvable, but the record never authorized them.")
        for aid in recon.orphaned[:limit]:
            out.append(f"     {aid}")
    if recon.dangling_grants:
        out.append(f"  !! {len(recon.dangling_grants)} grant(s) persist with nothing pending —")
        out.append("     a durable standing authorization. Investigate before granting anything.")
        for aid in recon.dangling_grants[:limit]:
            out.append(f"     {aid}")
    out.append("")
    out.append("  python -m freight_fleet.cli approvals reconcile   # the full report")
    out.append("")
    return "\n".join(out)


def cmd_approvals(args: argparse.Namespace) -> int:
    """List, reconcile, grant, reject or abandon. Every verb that changes
    anything is a thin printer over `governance.gate` — the replay lives at the
    seam, not in the front door, so the console cannot drift from the CLI about
    what approving means."""
    from .agents.fleet import _TOOL_FNS
    from .governance.gate import (
        abandon_stranded,
        execute_approved,
        open_store,
        reconcile,
        reject_approved,
    )

    store = open_store(_APPROVALS_PATH)

    if args.action in {"list", "reconcile"}:
        recon = reconcile(Ledger(_LEDGER_PATH), store)
        pending = store.pending() if store is not None else {}

        if args.action == "reconcile":
            print("\n  RECONCILE — the record against the queue\n")
            print(f"    ledger    {_LEDGER_PATH}")
            print(f"    store     {_APPROVALS_PATH}"
                  f"{'' if recon.store_readable else '   (UNREADABLE)'}")
            print(f"\n    {len(recon.awaiting)} awaiting · {len(recon.stranded)} stranded · "
                  f"{len(recon.orphaned)} orphaned · "
                  f"{len(recon.dangling_grants)} dangling grant(s)")
            if not recon.diverged:
                print("\n  The record and the queue agree.\n")
                return 0
            print(_stranded_block(recon, limit=1000))
            # Exit 1 so this doubles as a healthcheck and a CI assertion.
            return 1

        if store is None:
            # "no actions awaiting approval" would be a claim about a queue this
            # process could not read. Say nothing about the queue at all.
            print("\n  the approval store could not be read")
        elif not pending:
            print("\n  no actions awaiting approval")
        else:
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
        # An empty queue is only good news if the record agrees with it.
        print(_stranded_block(recon) or "")
        return 0

    if not args.approval_id:
        print("  approval id required")
        return 1
    aid = args.approval_id

    if store is None:
        print(f"  the approval store at {_APPROVALS_PATH} could not be read; refusing to "
              "decide anything against a queue this process cannot see")
        return 1

    if args.action == "abandon":
        res = abandon_stranded(aid, ledger=Ledger(_LEDGER_PATH), approvals=store,
                               source="approval-cli", note=args.note)
        if res.status == "not_held":
            print(f"  no held row in the ledger carries id {aid}")
            return 1
        if res.status == "already_resolved":
            print(f"  {aid} is already resolved in the record; nothing to abandon")
            return 1
        if res.status == "still_pending":
            print(f"  {aid} is still in the approval store — reject it instead:")
            print(f"    python -m freight_fleet.cli approvals reject {aid}")
            return 1
        print(f"  abandoned {aid} ({res.tool} {res.path}) — {res.detail}")
        print("  one ledger row was appended. Nothing was written to the workspace.")
        return 0

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
        print(f"  {res.tool} has no executable body wired - the hold stands")
        return 1
    if res.status == "gate_refused":
        print(f"  gate refused the replay: {(res.gate or {}).get('status')} - not executing")
        return 1
    print(f"  executed {res.tool} -> {res.path} ({res.bytes} bytes, {res.detail})")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Transcribe `workspace/raw/` into `workspace/inbox/` markdown.

    `ingest` is imported HERE rather than at module scope so `catalog`, `ledger`
    and `approvals` keep starting without paying for it — the same reason every
    other model-touching command in this file defers its imports. (The module
    itself imports `google.genai` lazily too, so even this import is cheap; the
    rule is worth keeping uniform anyway.)

    Exits 1 if anything failed, because a scheduled or scripted ingest that
    transcribed twenty-five of twenty-six documents and returned 0 would be a
    silent hole in the inbox.
    """
    from pathlib import Path

    from . import ingest as ingest_mod

    workspace = Path(os.environ.get("FREIGHT_WORKSPACE_ROOT", "./workspace")).resolve()
    report = ingest_mod.run(
        workspace,
        ingest_mod.transcribe_with_genai(args.model),
        only=args.only,
        force=args.force,
        dry_run=args.dry_run,
        model_label=args.model,
    )

    print(f"\n  INGEST — raw/ -> inbox/ under {workspace}")
    if not report.planned:
        print("\n  nothing to ingest (no raw/ originals matched)."
              " Seed with: python scripts/seed_workspace.py --all\n")
        return 0
    print()

    # One line per PLANNED item, in plan order, whatever happened to it: the
    # operator's mental model is the plan, and a run that reordered its output
    # by outcome would be unreadable against the dry run they just read.
    outcome = {it.source: ("wrote", "") for it in report.written}
    outcome.update({it.source: ("skip", "exists; --force to overwrite") for it in report.skipped})
    outcome.update({it.source: ("FAIL", why) for it, why in report.failed})
    for item in report.planned:
        src = item.source.relative_to(workspace).as_posix()
        dst = item.target.relative_to(workspace).as_posix()
        if args.dry_run:
            print(f"  plan  {src} -> {dst}{'  [exists]' if item.exists else ''}")
            continue
        verb, detail = outcome.get(item.source, ("?", "not reached"))
        print(f"  {verb:5} {src} -> {dst}{'  ' + detail if detail else ''}")

    if args.dry_run:
        print(f"\n  {len(report.planned)} planned, "
              f"{sum(1 for it in report.planned if it.exists)} already in inbox/. "
              "Nothing was written.\n")
        return 0
    print(f"\n  {len(report.written)} written, {len(report.skipped)} skipped (exists), "
          f"{len(report.failed)} failed\n")
    return 1 if report.failed else 0


def cmd_console(args: argparse.Namespace) -> int:
    """Serve the operator console. No credentials, no model — every page is a
    read of the ledger, the approval store, the catalog or a committed run."""
    import uvicorn

    print(f"\n  operator console on http://{args.host}:{args.port}  (no credentials needed)\n")
    uvicorn.run("freight_fleet.console:app", host=args.host, port=args.port, log_level="info")
    return 0


def cmd_chat_users(args: argparse.Namespace) -> int:
    """Mint demo-login credentials for the chat surface (DEPLOY.md §4d).

    Prints the `{username: scrypt-hash}` table that becomes the FREIGHT_CHAT_USERS
    secret, then the passwords ONCE, as comments, for the submission form. The
    passwords exist nowhere else: this command is the only time they are plain.
    """
    import json

    from .access import mint_users

    table, passwords = mint_users(args.usernames)
    print(json.dumps(table, indent=2))
    print()
    print("# passwords — shown once, for the submission form only:")
    for user, password in passwords.items():
        print(f"#   {user}: {password}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """`argv` is a parameter so the tests can drive a whole command in-process
    (`main(["ingest", "--dry-run"])`) instead of asserting on a subprocess's
    stdout. `None` keeps the normal `sys.argv` behaviour."""
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

    p_appr = sub.add_parser("approvals", help="list, reconcile, grant, reject or abandon")
    p_appr.add_argument("action", choices=["list", "reconcile", "grant", "reject", "abandon"])
    p_appr.add_argument("approval_id", nargs="?")
    p_appr.add_argument("--note", default="", help="operator note recorded with an abandon")
    p_appr.set_defaults(fn=cmd_approvals)

    p_ingest = sub.add_parser("ingest", help="transcribe workspace/raw originals into inbox markdown")
    p_ingest.add_argument("--only", default=None,
                          help="glob against the raw-relative path, e.g. 'inbox/scan_001*'")
    p_ingest.add_argument("--force", action="store_true", help="overwrite inbox targets that exist")
    p_ingest.add_argument("--dry-run", action="store_true", help="print the plan, call no model")
    p_ingest.add_argument("--model", default=os.environ.get("FREIGHT_MODEL", "gemini-3.7-flash"),
                          help="Gemini model that transcribes each document (default: $FREIGHT_MODEL)")
    p_ingest.set_defaults(fn=cmd_ingest)

    p_users = sub.add_parser("chat-users", help="mint demo-login credentials for the chat surface")
    p_users.add_argument("usernames", nargs="+", help="e.g. judge1 judge2 judge3")
    p_users.set_defaults(fn=cmd_chat_users)

    p_console = sub.add_parser("console", help="serve the operator console (no credentials)")
    p_console.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    p_console.add_argument("--host", default="127.0.0.1")
    p_console.set_defaults(fn=cmd_console)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
