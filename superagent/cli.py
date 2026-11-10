from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .agents import ATTENTION_STATES, HOOK_SCRIPT, SessionState, settings_block
from .brief import BriefSettings, build_brief
from .config import DB_FILENAME
from .digest import build_digest, send_digest
from .domain import ReportState, ScopeClass, Trigger
from .ledger import Item, ItemState, Ledger, LedgerError
from .reporting import identity_from_workdir, submit_report
from .router import Assessor
from .runtime import open_runtime, save_brief, tick
from .watchdog import DriftJudge

_WORKER_STATES = [state.value for state in ReportState]
_SCOPE_CLASSES = [item.value for item in ScopeClass]


def setup_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", type=Path, default=None, help="directory holding superagent.toml and the database")
    commands = parser.add_subparsers(dest="superagent_command", required=True)
    commands.add_parser("tick", help="assess candidates, check every worker, relay messages to people")
    commands.add_parser("status", help="list open tickets")
    ingest = commands.add_parser("ingest", help="feed one environment event through the gate")
    ingest.add_argument("--source", required=True)
    ingest.add_argument("--thread", required=True)
    ingest.add_argument("--actor", required=True)
    ingest.add_argument("--text", required=True)
    ingest.add_argument("--id", default=None)
    ingest.add_argument("--channel", default="")
    ingest.add_argument("--addressed", action="store_true")
    ingest.add_argument("--bot", action="store_true")
    digest = commands.add_parser(
        "digest", help="summarise recent work, waiting questions and what record-mode rules would do"
    )
    digest.add_argument("--hours", type=float, default=24.0)
    digest.add_argument("--send", action="store_true", help="queue it for the [digest] channel, sent on the next tick")
    cancel = commands.add_parser("cancel", help="stop a ticket and its worker, and tell the people on it")
    cancel.add_argument("ticket")
    cancel.add_argument("--by", default="an operator")
    outbox = commands.add_parser("outbox", help="list messages not delivered yet, or drop one")
    outbox.add_argument("--drop", type=int, default=None, metavar="SEQ", help="stop retrying this message")
    outbox.add_argument("--reason", default="dropped by an operator")
    brief = commands.add_parser("brief", help="what is open, waiting, gone or untouched, since the last brief")
    brief.add_argument("--send", action="store_true", help="also queue it for [channels] brief")
    agents = commands.add_parser("agents", help="every Claude Code session the agents connectors see")
    agents.add_argument("--json", action="store_true")
    agents.add_argument("--hook-script", action="store_true", help="print the hook script to put on a host")
    agents.add_argument("--hook-settings", action="store_true", help="print the hooks block for a host's settings")
    ledger = commands.add_parser("ledger", help="list, show, close, ignore or snooze tracked items")
    ledger_commands = ledger.add_subparsers(dest="ledger_command", required=True)
    listing = ledger_commands.add_parser("list", help="open items, or every item with --all")
    listing.add_argument("--all", action="store_true")
    listing.add_argument("--connector", default="")
    listing.add_argument("--json", action="store_true")
    show = ledger_commands.add_parser("show", help="one item and its full history")
    show.add_argument("item", help="a handle such as it-7f3k, or a full id")
    close = ledger_commands.add_parser("close", help="mark an item done")
    close.add_argument("item")
    close.add_argument("--reason", default="closed by the owner")
    ignore = ledger_commands.add_parser("ignore", help="stop tracking an item, with a reason")
    ignore.add_argument("item")
    ignore.add_argument("--reason", required=True)
    accept = ledger_commands.add_parser("accept", help="apply a held-back snapshot that shrank a lot")
    accept.add_argument("connector")
    snooze = ledger_commands.add_parser("snooze", help="hide an item from reconciliation until a date")
    snooze.add_argument("item")
    snooze.add_argument("--until", required=True, help="ISO date or date and time, local time")
    report = commands.add_parser("report", help="report status from a worker directory, for agents outside Hermes")
    report.add_argument("--state", required=True, choices=_WORKER_STATES)
    report.add_argument("--summary", required=True)
    report.add_argument("--question", default="")
    report.add_argument("--scope-class", default="", choices=["", *_SCOPE_CLASSES], help="kastle-build-loop only")


def run(
    args: argparse.Namespace,
    *,
    default_home: Callable[[], Path] | None = None,
    judge: DriftJudge | None = None,
    assessor: Assessor | None = None,
) -> int:
    if args.superagent_command == "report":
        print(
            json.dumps(
                submit_report(
                    identity_from_workdir(Path.cwd()),
                    ReportState(args.state),
                    args.summary,
                    args.question,
                    args.scope_class,
                )
            )
        )
        return 0
    home = args.home or (default_home() if default_home else None)
    if home is None:
        print("--home is required outside Hermes", file=sys.stderr)
        return 2
    if args.superagent_command == "agents" and (args.hook_script or args.hook_settings):
        if args.hook_script:
            print(HOOK_SCRIPT.read_text(encoding="utf-8"), end="")
        else:
            print(json.dumps(settings_block(), indent=2))
        return 0
    if args.superagent_command == "ledger":
        return run_ledger(args, Ledger(home / DB_FILENAME))
    runtime = open_runtime(home)
    if args.superagent_command == "digest":
        text = build_digest(runtime.store, time.time() - args.hours * 3600, args.hours)
        if args.send:
            send_digest(runtime.store, runtime.config.digest_channel, text)
        print(text)
        return 0
    if args.superagent_command == "cancel":
        ticket = runtime.router.cancel(args.ticket, args.by)
        print(json.dumps({"ticket": ticket.id, "status": ticket.status.value}))
        return 0
    if args.superagent_command == "brief":
        now = time.time()
        if args.send:
            if not runtime.config.brief_channel:
                print("set brief under [channels] to send the brief", file=sys.stderr)
                return 1
            save_brief(runtime, f"manual-{int(now)}", now)
            print("queued for", runtime.config.brief_channel)
            return 0
        settings = BriefSettings(runtime.config.stale_after_seconds, runtime.config.brief_timezone)
        since = runtime.ledger.last_brief(now) or now - 86400
        print(build_brief(runtime.ledger, runtime.config.connectors, since, now, settings).text)
        return 0
    if args.superagent_command == "agents":
        names = {spec.name for spec in runtime.config.connectors if spec.type == "agents"}
        sessions = board(runtime.ledger, names)
        if args.json:
            print(json.dumps([asdict(item) for item in sessions], indent=1))
        else:
            now = time.time()
            for item in sessions:
                minutes = (now - item.last_changed) / 60
                state = item.meta.get("state", "?")
                line = f"{item.handle}  {state:<10} {minutes:>5.0f}m  {item.connector:<14} {item.title}"
                print(line + (f"\n        {item.excerpt[:160]}" if item.excerpt else ""))
        return 0
    if args.superagent_command == "outbox":
        if args.drop is not None:
            if not runtime.store.drop_message(args.drop, args.reason):
                print(f"no undelivered message {args.drop}", file=sys.stderr)
                return 1
            print(json.dumps({"dropped": args.drop, "reason": args.reason}))
            return 0
        messages = [
            {"seq": m.seq, "ticket": m.ticket_id, "channel": m.channel, "attempts": m.attempts, "text": m.text[:200]}
            for m in runtime.store.pending_outbox()
        ]
        print(json.dumps(messages, indent=1))
        return 0
    if args.superagent_command == "status":
        print(
            json.dumps(
                {
                    "tickets": runtime.router.status(),
                    "undelivered": runtime.store.undelivered_count(),
                    "alerts": runtime.store.active_alerts(),
                    "sources": [asdict(source) for source in runtime.ledger.sources()],
                },
                indent=1,
            )
        )
        return 0
    if args.superagent_command == "ingest":
        trigger = Trigger(
            id=args.id or f"cli-{uuid.uuid4().hex[:12]}",
            source=args.source,
            thread=args.thread,
            actor=args.actor,
            text=args.text,
            channel=args.channel or args.thread,
            addressed=args.addressed,
            is_bot=args.bot,
        )
        disposition = runtime.router.ingest(trigger)
        print(
            json.dumps(
                {
                    "trigger": trigger.id,
                    "outcome": disposition.outcome.value,
                    "reason": disposition.reason,
                    "tickets": list(disposition.ticket_ids),
                }
            )
        )
        return 0
    deadline = arm_deadline(runtime.config.tick_deadline_seconds)
    try:
        result = tick(runtime, judge, assessor)
    finally:
        deadline.cancel()
    print(
        json.dumps(
            {
                "new_tickets": [ticket.id for ticket in result.new_tickets],
                "checks": [
                    {
                        "ticket": check.ticket_id,
                        "assignment": check.assignment_id,
                        "verdict": check.verdict.value,
                        "action": check.action.value,
                        "reason": check.reason,
                    }
                    for check in result.checks
                ],
                "delivered": [{"channel": channel, "text": text} for _, channel, text in result.delivered],
                "errors": list(result.errors),
                "warnings": list(result.warnings),
                "skipped": result.skipped,
            }
        )
    )
    return 1 if result.errors else 0


def arm_deadline(seconds: float) -> threading.Timer:
    def expire() -> None:
        print(json.dumps({"errors": [f"tick: ran past its {seconds:g}s deadline and was stopped"]}), flush=True)
        os._exit(3)

    timer = threading.Timer(seconds, expire)
    timer.daemon = True
    timer.start()
    return timer


def board(ledger: Ledger, connectors: set[str]) -> list[Item]:
    sessions = [item for name in sorted(connectors) for item in ledger.items(open_only=True, connector=name)]
    order = {state.value: rank for rank, state in enumerate(SessionState)}
    attention = {state.value for state in ATTENTION_STATES}
    return sorted(
        sessions,
        key=lambda item: (item.meta.get("state") not in attention, order.get(item.meta.get("state", ""), 99)),
    )


def run_ledger(args: argparse.Namespace, ledger: Ledger) -> int:
    try:
        if args.ledger_command == "list":
            items = ledger.items(open_only=not args.all, connector=args.connector)
            if args.json:
                print(json.dumps([asdict(item) for item in items], indent=1))
            else:
                for item in items:
                    print(f"{item.handle}  {item.state.value:<8} {item.connector:<12} {item.title}")
            return 0
        if args.ledger_command == "show":
            item = ledger.get(args.item)
            history = [asdict(event) for event in ledger.history(item.id)]
            print(json.dumps({"item": asdict(item), "history": history}, indent=1))
            return 0
        if args.ledger_command == "accept":
            ledger.accept_next_snapshot(args.connector)
            print(json.dumps({"accepted": args.connector, "applies": "on its next poll"}))
            return 0
        if args.ledger_command == "close":
            item = ledger.move(args.item, ItemState.DONE, "owner", args.reason)
        elif args.ledger_command == "ignore":
            item = ledger.move(args.item, ItemState.IGNORED, "owner", args.reason)
        else:
            item = ledger.snooze(args.item, datetime.fromisoformat(args.until).timestamp(), "owner")
    except (LedgerError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({"item": item.handle, "state": item.state.value, "snoozed_until": item.snoozed_until}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="superagent")
    setup_parser(parser)
    return run(parser.parse_args(argv))
