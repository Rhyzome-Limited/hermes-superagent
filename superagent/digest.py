from __future__ import annotations

from collections import Counter

from .domain import Admission, DigestItem, ReportState, TicketStatus
from .router import chat_safe, shorten, task_label
from .store import Store

ITEMS_PER_SECTION = 5
ITEM_WORDS = 25
RECORDED_PREFIX = "recorded by "
DIGEST_TICKET = "digest"


class DigestError(ValueError):
    pass


def send_digest(store: Store, channel: str, text: str) -> None:
    if not channel:
        raise DigestError("set channel under [digest] in superagent.toml to send the digest")
    store.queue_message(DIGEST_TICKET, channel, text)


def build_digest(store: Store, since: float, hours: float) -> str:
    finished = store.final_reports_since(since)
    escalated = store.escalations_since(since)
    waiting = [item for item in store.live_tickets() if item.status is TicketStatus.WAITING]
    running = sum(1 for item in store.live_tickets() if item.status is TicketStatus.OPEN)
    queued = sum(1 for item in store.live_tickets() if item.status is TicketStatus.QUEUED)
    recorded = store.assessments_since(since, RECORDED_PREFIX)
    ignored = store.admission_counts_since(since, Admission.IGNORE)
    undelivered = store.undelivered_count()
    lines = [f"Superagent, last {hours:g} hours"]
    done = [item for item, state in finished if state is ReportState.READY]
    failed = [item for item, state in finished if state is not ReportState.READY]
    lines += _section("Done", done)
    lines += _section("Failed or dropped", failed)
    lines += _section("Waiting on you", [_waiting_item(store, ticket.id, ticket.goal) for ticket in waiting])
    lines += _section("Escalated", escalated)
    lines += _rule_section(recorded)
    if running:
        lines.append(f"Running: {running}")
    if queued:
        lines.append(f"Queued: {queued}")
    if ignored:
        counts = ", ".join(f"{source} {count}" for source, count in Counter(ignored).most_common())
        lines.append(f"Ignored events: {counts}")
    if undelivered:
        lines.append(f"Undelivered messages: {undelivered}")
    if len(lines) == 1:
        lines.append("Nothing happened.")
    return chat_safe("\n".join(lines))


def _waiting_item(store: Store, ticket_id: str, goal: str) -> DigestItem:
    reports = store.ticket_reports(ticket_id)
    question = reports[-1].question if reports and reports[-1].question else "waiting for an answer"
    return DigestItem(ticket_id, goal, question)


def _section(title: str, items: list[DigestItem]) -> list[str]:
    if not items:
        return []
    lines = [f"{title} ({len(items)}):"]
    lines += [
        f"- {task_label(item.ticket_id, item.goal)} {shorten(item.text, ITEM_WORDS)}"
        for item in items[:ITEMS_PER_SECTION]
    ]
    if len(items) > ITEMS_PER_SECTION:
        lines.append(f"- and {len(items) - ITEMS_PER_SECTION} more")
    return lines


def _rule_section(recorded: list[tuple[str, str]]) -> list[str]:
    if not recorded:
        return []
    by_rule: dict[str, list[str]] = {}
    for assessment, source in recorded:
        rule, _, goal = assessment[len(RECORDED_PREFIX) :].partition(": ")
        by_rule.setdefault(rule, []).append(f"{shorten(goal, ITEM_WORDS)} ({source})")
    lines = ["Rules in record mode would have started:"]
    for rule, goals in sorted(by_rule.items()):
        lines.append(f"- {rule}, {len(goals)}: " + "; ".join(goals[:ITEMS_PER_SECTION]))
    lines.append('Switch a rule to mode = "act" once these look right.')
    return lines
