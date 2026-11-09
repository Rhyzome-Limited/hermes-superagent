from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .agents import redact
from .domain import (
    DEFAULT_LOOP,
    DEFAULT_MAX_RUNNING,
    HUMAN_FACING_REPORT_STATES,
    Admission,
    Assignment,
    Loop,
    Mandate,
    Report,
    ReportState,
    Strength,
    Ticket,
    TicketStatus,
    Trigger,
)
from .framing import frame_trigger, frame_untrusted
from .gate import RuleMode, Rules, SemanticRule, SourcePolicy, gate
from .store import OutboxMessage, Store
from .worker import WorkerBackend

logger = logging.getLogger(__name__)

CHAT_WORD_LIMIT = 100
LABEL_WORDS = 6
MAX_ASSESS_BATCH = 20
ORDERED_RETRIES = 3
ASSESS_ATTEMPTS = 3
OFFER_VALID_SECONDS = 3600.0
_ATTACHMENT_TAG = re.compile(r"media:", re.IGNORECASE)
_DIRECTIVE_OPENING = re.compile(r"\[(?=\[)")
_MENTION = re.compile(r"@(?=everyone|here)|<@|<#")
_IMAGE_OPENING = re.compile(r"!(?=\[)")
# No part runs past the next opening bracket, so a run of unclosed ones costs linear time. A nested image or link
# these miss still loses its URL to _URL.
_IMAGE_TAG = re.compile(r"!\[[^\[\]\n]*\]\([^()\n]*\)|<img\b[^<>]*>", re.IGNORECASE)
_MARKDOWN_LINK = re.compile(r"\[([^\[\]\n]*)\]\([^()\n]*\)")
_GITHUB_ITEM = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([\w.-]+/[\w.-]+)/(?:pull|issues)/(\d+)(?:[/#?][^\s)\]]*)?", re.IGNORECASE
)
# A full stop, or one of the stops IDNA also reads as a dot.
_DOT = r"[.\u3002\uff0e\uff61]"
# A host as chat apps auto-link it without a scheme: attacker.example, a.b.co.uk, README.md, 10.0.0.1. It starts
# only at the first label of a dotted run, which covers any host inside the run and keeps the match linear.
_HOST = (
    rf"(?<![\w-])(?<![\w-]{_DOT})"
    rf"(?:(?:[\w-]+{_DOT})+[^\W\d_][\w-]+|\d{{1,3}}(?:{_DOT}\d{{1,3}}){{3}})(?![\w-])"
)
_URL = re.compile(rf"(?:\b[a-z][a-z0-9+.-]{{0,31}}://|\bwww\.)\S+|{_HOST}(?::\d+)?[/?#]\S*", re.IGNORECASE)
_BARE_HOST = re.compile(_HOST)
_HOST_DOT = re.compile(_DOT)
# A Kastle repo named on its own or as owner/name: a hyphen continues a repo name, so kastle-push is another repo.
_KASTLE_REPO = re.compile(r"(?<![\w-])kastle(?:-mobile)?(?![\w-])", re.IGNORECASE)
_KASTLE_TICKET = re.compile(r"(?<![\w-])CMPY-\d+(?!\w)", re.IGNORECASE)
MAX_REPO_CHARS = 200


def route_loop(requested: str, goal: str, repo: str = "") -> Loop:
    """The loop a ticket runs: the router's pick from a fixed list, except that work on the Kastle repos or a
    CMPY ticket always runs kastle-build-loop, whatever was asked."""
    try:
        loop = Loop(requested)
    except ValueError as error:
        raise ValueError(f"loop must be one of {', '.join(item.value for item in Loop)}, got {requested!r}") from error
    named = f"{repo}\n{goal}"
    if _KASTLE_REPO.search(named) or _KASTLE_TICKET.search(named):
        return Loop.KASTLE_BUILD
    return loop


class Outcome(StrEnum):
    DISPATCHED = "dispatched"
    PASSED_THROUGH = "passed_through"
    CANDIDATE = "candidate"
    IGNORED = "ignored"
    DUPLICATE = "duplicate"
    ADMITTED = "admitted"


@dataclass(frozen=True)
class Disposition:
    outcome: Outcome
    reason: str
    ticket_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Assessment:
    trigger_id: str
    rule: str
    goal: str


Sender = Callable[[str, str], None]


class Assessor(Protocol):
    def assess(self, batch: Sequence[Trigger], rules: Sequence[SemanticRule]) -> Sequence[Assessment]: ...


class NotAdmittedError(PermissionError):
    pass


class Router:
    def __init__(
        self, store: Store, rules: Rules, backend: WorkerBackend, max_running: int = DEFAULT_MAX_RUNNING
    ) -> None:
        self.store = store
        self.rules = rules
        self.backend = backend
        self.max_running = max_running

    def ingest(self, trigger: Trigger, *, auto_dispatch: bool = True) -> Disposition:
        decision = gate(trigger, self.rules)
        if not self.store.record_trigger(trigger, decision.admission, decision.reason, self.rules.version):
            return Disposition(Outcome.DUPLICATE, "trigger already recorded")
        subscribers = [
            ticket_id
            for ticket_id in self.store.subscribers(trigger.thread)
            if self.store.get_ticket(ticket_id).mandate.source == trigger.source
        ]
        if subscribers and self._may_reach_workers(trigger):
            return self._pass_through(trigger, subscribers)
        if decision.admission is Admission.IGNORE:
            return Disposition(Outcome.IGNORED, decision.reason)
        if decision.admission is Admission.CANDIDATE:
            return Disposition(Outcome.CANDIDATE, decision.reason)
        if not auto_dispatch:
            return Disposition(Outcome.ADMITTED, decision.reason)
        self.store.mark_assessed(trigger.id, "dispatched")
        ticket = self._open_ticket(trigger, trigger.text, Strength.EXPLICIT, route_loop(DEFAULT_LOOP, trigger.text))
        return Disposition(Outcome.DISPATCHED, decision.reason, (ticket.id,))

    def dispatch(self, trigger_id: str, goal: str, loop: str = DEFAULT_LOOP, repo: str = "") -> Ticket:
        repo = repo.strip()
        if len(repo) > MAX_REPO_CHARS:
            raise ValueError(f"repo must be at most {MAX_REPO_CHARS} characters")
        routed = route_loop(loop, goal, repo)
        trigger = self._admitted_trigger(trigger_id)
        if self.store.claim_offered_trigger(trigger_id, "dispatched", OFFER_VALID_SECONDS):
            return self._open_ticket(trigger, goal, Strength.EXPLICIT, routed, repo)
        existing = self.store.ticket_for_trigger(trigger_id)
        if existing is not None and self.store.assessment_of(trigger_id) == "dispatched":
            return existing
        raise NotAdmittedError(f"trigger {trigger_id} was already used or is not an offered request")

    def forward(self, trigger_id: str, ticket_id: str) -> Ticket:
        trigger, ticket = self._claim_for_ticket(trigger_id, ticket_id, f"forwarded to {ticket_id}")
        self.store.add_input(ticket_id, f"{trigger.source}:{trigger.actor}", frame_trigger(trigger), trusted=True)
        self._resume_if_waiting(ticket)
        return self.store.get_ticket(ticket_id)

    def cancel_from(self, trigger_id: str, ticket_id: str) -> Ticket:
        trigger, _ = self._claim_for_ticket(trigger_id, ticket_id, f"cancelled {ticket_id}")
        return self.cancel(ticket_id, trigger.actor or trigger.source)

    def cancel(self, ticket_id: str, by: str) -> Ticket:
        ticket = self.store.get_ticket(ticket_id)
        if ticket.status is TicketStatus.ENDED:
            raise ValueError(f"ticket {ticket_id} has already ended")
        self.store.end_ticket(ticket_id)
        label = task_label(ticket.id, ticket.goal)
        self.store.queue_message(ticket.id, ticket.channel, chat_safe(shorten(f"Cancelled {label} by {by}")))
        for assignment in self.store.running_assignments():
            if assignment.ticket_id == ticket_id and self.store.end_assignment(assignment.id):
                self.backend.stop(assignment)
        self.start_queued()
        return self.store.get_ticket(ticket_id)

    def _claim_for_ticket(self, trigger_id: str, ticket_id: str, assessment: str) -> tuple[Trigger, Ticket]:
        trigger = self._admitted_trigger(trigger_id)
        ticket = self.store.get_ticket(ticket_id)
        if ticket.status is TicketStatus.ENDED:
            raise ValueError(f"ticket {ticket_id} has ended, dispatch a new one")
        same_person = (
            bool(trigger.actor) and trigger.source == ticket.mandate.source and trigger.actor == ticket.mandate.actor
        )
        if trigger.channel != ticket.channel and not same_person:
            raise NotAdmittedError(f"trigger {trigger_id} comes from another conversation and another person")
        if not self.store.claim_offered_trigger(trigger_id, assessment, OFFER_VALID_SECONDS):
            raise NotAdmittedError(f"trigger {trigger_id} was already used or is not an offered request")
        return trigger, ticket

    def assess_candidates(self, assessor: Assessor) -> list[Ticket]:
        batch = self.store.pending_candidates(MAX_ASSESS_BATCH)
        active_rules = list(self.rules.semantic)
        if not batch:
            return []
        try:
            decided = assessor.assess(batch, active_rules)
        except Exception:
            self.store.record_assessment_failure([trigger.id for trigger in batch], ASSESS_ATTEMPTS)
            raise
        assessments = {item.trigger_id: item for item in decided}
        modes = {rule.name: rule.mode for rule in active_rules}
        tickets: list[Ticket] = []
        for trigger in batch:
            assessment = assessments.get(trigger.id)
            if assessment is None:
                self.store.mark_assessed(trigger.id, "quiet")
                continue
            if modes.get(assessment.rule) is not RuleMode.ACT:
                self.store.mark_assessed(trigger.id, f"recorded by {assessment.rule}: {assessment.goal}")
                continue
            self.store.mark_assessed(trigger.id, f"accepted by {assessment.rule}")
            loop = route_loop(DEFAULT_LOOP, assessment.goal)
            tickets.append(self._open_ticket(trigger, assessment.goal, Strength.INFERRED, loop))
        return tickets

    def relay(self, send: Sender | None = None, only_channel: str = "") -> list[tuple[str, str, str]]:
        self.store.move_to_outbox(_render_report, _render_escalation)
        delivered: list[tuple[str, str, str]] = []
        held: set[str] = set()
        backing_off: dict[str, list[OutboxMessage]] = {}
        for message in self.store.pending_outbox():
            if message.channel in held or (only_channel and message.channel != only_channel):
                continue
            if not message.due:
                if message.attempts < ORDERED_RETRIES:
                    held.add(message.channel)
                else:
                    backing_off.setdefault(message.channel, []).append(message)
                continue
            for earlier in backing_off.pop(message.channel, []):
                self._deliver(earlier, send, delivered)
            if not self._deliver(message, send, delivered) and message.attempts + 1 < ORDERED_RETRIES:
                held.add(message.channel)
        return delivered

    def _deliver(self, message: OutboxMessage, send: Sender | None, delivered: list[tuple[str, str, str]]) -> bool:
        if not self.store.is_pending(message.seq):
            return True
        if send is not None:
            try:
                send(message.channel, message.text)
            except Exception as error:
                self.store.record_send_failure(message.seq)
                logger.warning("send to %s failed, retrying later: %s", message.channel, error)
                return False
        self.store.mark_sent(message.seq)
        delivered.append((message.ticket_id, message.channel, message.text))
        return True

    def status(self) -> list[dict[str, str]]:
        lines = []
        for ticket in self.store.live_tickets():
            reports = self.store.ticket_reports(ticket.id)
            lines.append(
                {
                    "ticket": ticket.id,
                    "goal": shorten(ticket.goal),
                    "loop": ticket.loop,
                    "status": ticket.status.value,
                    "state": reports[-1].state.value if reports else "starting",
                    "question": reports[-1].question if reports and ticket.status is TicketStatus.WAITING else "",
                }
            )
        return lines

    def is_allowed(self, trigger: Trigger) -> bool:
        rules = self.rules.sources.get(trigger.source)
        return rules is not None and trigger.actor in rules.allow

    def start_worker(self, ticket: Ticket) -> Assignment | None:
        created = self.store.create_assignment(ticket.id, self.max_running)
        if created is None:
            return None
        return self._launch(ticket, *created)

    def start_queued(self) -> list[Assignment]:
        """Start queued tickets, oldest first, while fewer than max_running workers run."""
        started: list[Assignment] = []
        while (claimed := self.store.start_next_queued(self.max_running)) is not None:
            started.append(self._launch(*claimed))
        return started

    def _launch(self, ticket: Ticket, assignment: Assignment, token: str) -> Assignment:
        pending = self.store.pending_inputs(ticket.id)
        reports = self.store.ticket_reports(ticket.id)
        parts = [text for _, _, text, _ in pending]
        if reports:
            last = reports[-1]
            history = f"{last.summary}\nIt asked: {last.question}" if last.question else last.summary
            parts.insert(0, frame_untrusted("what the last worker reported", history, keep_tail=True))
        pid = self.backend.start(ticket, assignment, token, "\n".join(parts))
        self.store.set_assignment_pid(assignment.id, pid)
        self.store.mark_inputs_delivered([seq for seq, _, _, _ in pending])
        return self.store.get_assignment(assignment.id)

    def _admitted_trigger(self, trigger_id: str) -> Trigger:
        trigger = self.store.get_trigger(trigger_id)
        if trigger is None:
            raise NotAdmittedError(f"unknown trigger: {trigger_id}")
        if self.store.admission_of(trigger_id) is not Admission.ACT:
            raise NotAdmittedError(f"trigger {trigger_id} was not admitted by the gate")
        if gate(trigger, self.rules).admission is not Admission.ACT:
            raise NotAdmittedError(f"trigger {trigger_id} is no longer allowed by the current rules")
        return trigger

    def _open_ticket(self, trigger: Trigger, goal: str, strength: Strength, loop: Loop, repo: str = "") -> Ticket:
        mandate = Mandate(self.rules.version, goal, strength, trigger.actor, trigger.source, trigger.id)
        source = self.rules.sources.get(trigger.source)
        channel = source.notify if source is not None and source.notify else trigger.channel
        ticket, created = self.store.create_ticket(goal, mandate, channel, trigger.thread, loop.value, repo)
        if not created:
            return ticket
        self.start_worker(ticket)
        return self.store.get_ticket(ticket.id)

    def _pass_through(self, trigger: Trigger, subscribers: list[str]) -> Disposition:
        framed = frame_trigger(trigger)
        trusted = self.is_allowed(trigger)
        for ticket_id in subscribers:
            self.store.add_input(ticket_id, f"{trigger.source}:{trigger.actor}", framed, trusted=trusted)
            if trusted:
                self._resume_if_waiting(self.store.get_ticket(ticket_id))
        self.store.mark_assessed(trigger.id, "passed through")
        return Disposition(Outcome.PASSED_THROUGH, "thread has a live subscriber", tuple(subscribers))

    def _resume_if_waiting(self, ticket: Ticket) -> None:
        resumed = self.store.resume_waiting_ticket(ticket.id, self.max_running)
        if resumed is not None:
            self._launch(ticket, *resumed)

    def _may_reach_workers(self, trigger: Trigger) -> bool:
        rules = self.rules.sources.get(trigger.source)
        if rules is None or rules.policy is SourcePolicy.IGNORE or trigger.is_bot:
            return False
        return self.is_allowed(trigger) or rules.pass_strangers


def chat_safe(text: str) -> str:
    quiet = _MENTION.sub(lambda match: match.group(0)[0] + "\u200b" + match.group(0)[1:], text)
    return _IMAGE_OPENING.sub("! ", _DIRECTIVE_OPENING.sub("[ ", _ATTACHMENT_TAG.sub("media ", quiet)))


def strip_links(text: str) -> str:
    """Worker text reaches chat with no image and nothing a chat app turns into a link, since a link preview fetches
    it. A GitHub pull request or issue keeps its reference as owner/repo#N. Every other URL is removed, and so is a
    bare host followed by a path, query or fragment. Any other host keeps its name with each dot written as [.], so
    a file name still reads as one: README[.]md, router[.]py:12."""
    text = _MARKDOWN_LINK.sub(r"\1", _IMAGE_TAG.sub("", text))
    text = _URL.sub("[link removed]", _GITHUB_ITEM.sub(r"\1#\2", text))
    return _BARE_HOST.sub(lambda match: _HOST_DOT.sub("[.]", match.group(0)), text)


def shorten(text: str, limit: int = CHAT_WORD_LIMIT) -> str:
    words = text.split()
    return text if len(words) <= limit else " ".join(words[:limit]) + " ..."


def task_label(ticket_id: str, goal: str) -> str:
    return f"[{shorten(goal, LABEL_WORDS)} · {ticket_id}]"


def _render_report(report: Report, goal: str) -> str | None:
    if report.state not in HUMAN_FACING_REPORT_STATES:
        return None
    text = report.question if report.question else report.summary
    prefix = "Done" if report.state is ReportState.READY else report.state.value.capitalize()
    return chat_safe(shorten(strip_links(redact(f"{prefix} {task_label(report.ticket_id, goal)}: {text}"))))


def _render_escalation(reason: str, ticket_id: str, goal: str) -> str:
    return chat_safe(shorten(strip_links(redact(f"Needs you {task_label(ticket_id, goal)}: {reason}"))))
