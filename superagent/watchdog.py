from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from .domain import Assignment, AssignmentStatus, RepairAction, Report, Ticket, Verdict
from .framing import frame_untrusted
from .router import Router
from .store import Store
from .worker import WorkerBackend, WorkerStopError

_NO_ASSIGNMENT = "none"
_LAST_WORDS_CHARS = 300


@dataclass(frozen=True)
class WatchdogPolicy:
    stall_after_seconds: float = 900.0
    repeat_limit: int = 3
    repeated_call_limit: int = 5
    nudge_budget: int = 1
    restart_budget: int = 1
    orphan_after_seconds: float = 30.0
    reap_grace_seconds: float = 30.0


@dataclass(frozen=True)
class Evidence:
    alive: bool
    silent_seconds: float
    reports: tuple[Report, ...]
    repeated_calls: int
    has_news: bool


@dataclass(frozen=True)
class Judgement:
    verdict: Verdict
    reason: str


@dataclass(frozen=True)
class CheckResult:
    assignment_id: str
    ticket_id: str
    verdict: Verdict
    action: RepairAction
    reason: str


class DriftJudge(Protocol):
    def judge(self, goal: str, reports: tuple[Report, ...]) -> Judgement: ...


class NoDriftJudge:
    def judge(self, goal: str, reports: tuple[Report, ...]) -> Judgement:
        return Judgement(Verdict.ON_TRACK, "no semantic judge configured")


def collect_evidence(store: Store, backend: WorkerBackend, assignment: Assignment, policy: WatchdogPolicy) -> Evidence:
    reports = tuple(store.reports_for(assignment.id))
    activity = store.recent_activity(assignment.id, policy.repeated_call_limit)
    moments = [assignment.started_at]
    if reports:
        moments.append(reports[-1].at)
    if activity:
        moments.append(activity[-1][1])
    last_seen = max(moments)
    fingerprints = {fingerprint for fingerprint, _ in activity}
    repeated = len(activity) if len(fingerprints) == 1 else 0
    last_checked = store.last_watchdog_at(assignment.id)
    has_news = bool(reports) and (last_checked is None or reports[-1].at > last_checked)
    return Evidence(backend.is_alive(assignment), time.time() - last_seen, reports, repeated, has_news)


def judge_deterministically(evidence: Evidence, policy: WatchdogPolicy) -> Judgement | None:
    if not evidence.alive:
        return Judgement(Verdict.STUCK, "worker process is gone without a final report")
    if evidence.silent_seconds > policy.stall_after_seconds:
        return Judgement(Verdict.STUCK, f"no report for {int(evidence.silent_seconds)}s")
    if evidence.repeated_calls >= policy.repeated_call_limit:
        return Judgement(Verdict.STUCK, f"same tool call and result {evidence.repeated_calls} times in a row")
    recent = [report.summary.strip().lower() for report in evidence.reports[-policy.repeat_limit :]]
    if len(recent) == policy.repeat_limit and len(set(recent)) == 1:
        return Judgement(Verdict.STUCK, f"last {policy.repeat_limit} reports are identical")
    return None


def choose_repair(store: Store, ticket: Ticket, evidence: Evidence, policy: WatchdogPolicy) -> RepairAction:
    nudges = store.repair_count(ticket.id, RepairAction.NUDGE)
    restarts = store.repair_count(ticket.id, RepairAction.RESTART)
    if evidence.alive and nudges < policy.nudge_budget:
        return RepairAction.NUDGE
    if restarts < policy.restart_budget:
        return RepairAction.RESTART
    return RepairAction.ESCALATE


def apply_repair(
    action: RepairAction, router: Router, ticket: Ticket, assignment: Assignment, judgement: Judgement
) -> None:
    if action is RepairAction.NUDGE:
        message = (
            f"Watchdog: {judgement.reason}. The goal is unchanged:\n"
            f"{frame_untrusted('ticket goal', ticket.goal)}\n"
            "Report real progress or report blocked with one concrete question."
        )
        router.store.add_input(ticket.id, "watchdog", message, trusted=True)
        return
    if not router.store.end_assignment(assignment.id):
        return
    router.backend.stop(assignment)
    if action is RepairAction.RESTART:
        router.start_worker(ticket)
        return
    router.store.end_ticket(ticket.id)


def check_once(
    router: Router,
    judge: DriftJudge | None = None,
    policy: WatchdogPolicy | None = None,
) -> list[CheckResult]:
    active_judge = judge or NoDriftJudge()
    active_policy = policy or WatchdogPolicy()
    _reap_finished(router, active_policy)
    results: list[CheckResult] = []
    for assignment in router.store.running_assignments():
        if assignment.pid is None and time.time() - assignment.started_at < active_policy.orphan_after_seconds:
            continue
        ticket = router.store.get_ticket(assignment.ticket_id)
        evidence = collect_evidence(router.store, router.backend, assignment, active_policy)
        if router.store.get_assignment(assignment.id).status is not AssignmentStatus.RUNNING:
            continue
        if not evidence.alive and (output := router.backend.last_output(assignment)):
            parked = _park_silent_exit(router, ticket, assignment, output)
            if parked is not None:
                results.append(parked)
            continue
        judgement = judge_deterministically(evidence, active_policy) or _judge_semantically(
            active_judge, ticket, evidence
        )
        action = (
            RepairAction.NONE
            if judgement.verdict is Verdict.ON_TRACK
            else choose_repair(router.store, ticket, evidence, active_policy)
        )
        router.store.add_watchdog_event(assignment.id, ticket.id, judgement.verdict.value, action, judgement.reason)
        if action is not RepairAction.NONE:
            try:
                apply_repair(action, router, ticket, assignment, judgement)
            except WorkerStopError as error:
                results.append(_escalate_unstoppable(router, ticket.id, assignment.id, error))
                router.store.end_ticket(ticket.id)
                router.store.mark_reaped(assignment.id)
                continue
        results.append(CheckResult(assignment.id, ticket.id, judgement.verdict, action, judgement.reason))
    for ticket in router.store.orphaned_tickets(active_policy.orphan_after_seconds):
        results.append(_repair_orphan(router, ticket, active_policy))
    router.start_queued()
    return results


def _reap_finished(router: Router, policy: WatchdogPolicy) -> None:
    for assignment in router.store.assignments_to_reap(policy.reap_grace_seconds):
        try:
            router.backend.stop(assignment)
        except WorkerStopError as error:
            _escalate_unstoppable(router, assignment.ticket_id, assignment.id, error)
        router.store.mark_reaped(assignment.id)


def _escalate_unstoppable(router: Router, ticket_id: str, assignment_id: str, error: WorkerStopError) -> CheckResult:
    reason = f"could not stop a worker, it may still be running: {error}"
    router.store.add_watchdog_event(assignment_id, ticket_id, Verdict.STUCK.value, RepairAction.ESCALATE, reason)
    return CheckResult(assignment_id, ticket_id, Verdict.STUCK, RepairAction.ESCALATE, reason)


def _repair_orphan(router: Router, ticket: Ticket, policy: WatchdogPolicy) -> CheckResult:
    reason = "open ticket has no running worker"
    restarts = router.store.repair_count(ticket.id, RepairAction.RESTART)
    action = RepairAction.RESTART if restarts < policy.restart_budget else RepairAction.ESCALATE
    router.store.add_watchdog_event(_NO_ASSIGNMENT, ticket.id, Verdict.STUCK.value, action, reason)
    if action is RepairAction.RESTART:
        router.start_worker(ticket)
    else:
        router.store.end_ticket(ticket.id)
    return CheckResult(_NO_ASSIGNMENT, ticket.id, Verdict.STUCK, action, reason)


def _park_silent_exit(router: Router, ticket: Ticket, assignment: Assignment, output: str) -> CheckResult | None:
    last_words = output[-_LAST_WORDS_CHARS:]
    question = f"The worker exited without a final report. Its last output: {last_words}\nShould it run again?"
    if not router.store.park_silent_exit(assignment.id, f"exited without a final report: {last_words}", question):
        return None
    reason = "worker exited without a final report, a person decides whether to run it again"
    router.store.add_watchdog_event(assignment.id, ticket.id, Verdict.STUCK.value, RepairAction.PARK, reason)
    return CheckResult(assignment.id, ticket.id, Verdict.STUCK, RepairAction.PARK, reason)


def _judge_semantically(judge: DriftJudge, ticket: Ticket, evidence: Evidence) -> Judgement:
    if not evidence.has_news:
        return Judgement(Verdict.ON_TRACK, "no new report to judge")
    return judge.judge(ticket.goal, evidence.reports)
