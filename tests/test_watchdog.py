from __future__ import annotations

import time

import pytest

from superagent.domain import Assignment, RepairAction, Report, ReportState, TicketStatus, Trigger, Verdict
from superagent.router import Router
from superagent.store import StaleAssignmentError
from superagent.watchdog import Judgement, WatchdogPolicy, check_once
from superagent.worker import WorkerStopError

from .conftest import FakeBackend, unlabelled

REQUEST = Trigger("t1", "chat", "th1", "alice", "fix the login bug", "chat:th1", True, False)


def report(router: Router, backend: FakeBackend, summary: str, times: int = 1) -> None:
    assignment = router.store.running_assignments()[0]
    for _ in range(times):
        router.store.add_report(assignment.id, backend.started[-1][2], ReportState.PROGRESS, summary)


def test_healthy_worker_is_left_alone(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    report(router, backend, "reading the auth module")
    result = check_once(router)[0]
    assert (result.verdict, result.action) == (Verdict.ON_TRACK, RepairAction.NONE)


def test_repeating_worker_is_nudged_then_restarted_then_escalated(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    report(router, backend, "still looking", 3)

    first = check_once(router)[0]
    assert (first.verdict, first.action) == (Verdict.STUCK, RepairAction.NUDGE)
    ticket_id = first.ticket_id
    assert "Watchdog" in router.store.take_inputs(ticket_id)[0][1]

    report(router, backend, "still looking", 1)
    second = check_once(router)[0]
    assert second.action is RepairAction.RESTART
    assert len(backend.started) == 2
    assert backend.stopped == [second.assignment_id]

    report(router, backend, "still looking", 3)
    third = check_once(router)[0]
    assert third.action is RepairAction.ESCALATE
    assert router.store.get_ticket(ticket_id).status is TicketStatus.ENDED
    assert router.store.running_assignments() == []
    assert [unlabelled(text) for _, _, text in router.relay()] == [
        f"Stuck: The worker sent the same report 3 times.\nThe watchdog restarted it once. It is stuck again.\n"
        f'You: check it, or send "cancel {ticket_id}".'
    ]


def test_restart_fences_out_the_old_worker(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    report(router, backend, "same", 3)
    check_once(router)
    report(router, backend, "same", 1)
    restarted = check_once(router)[0]
    with pytest.raises(StaleAssignmentError):
        router.store.add_report(restarted.assignment_id, backend.started[0][2], ReportState.READY, "late fake done")


def test_worker_cannot_report_done_while_being_stopped(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    report(router, backend, "same", 3)
    check_once(router)
    report(router, backend, "same", 1)
    token = backend.started[0][2]
    refused: list[bool] = []

    def report_on_sigterm(assignment_id: str) -> None:
        try:
            router.store.add_report(assignment_id, token, ReportState.READY, "all done, trust me")
            refused.append(False)
        except StaleAssignmentError:
            refused.append(True)

    backend.on_stop = report_on_sigterm
    check_once(router)
    assert refused == [True]
    assert all("trust me" not in text for _, _, text in router.relay())


def test_a_worker_that_cannot_be_stopped_escalates_without_a_second_worker(
    router: Router, backend: FakeBackend
) -> None:
    router.ingest(REQUEST)
    report(router, backend, "same", 3)
    check_once(router)
    report(router, backend, "same", 1)

    def denied(assignment_id: str) -> None:
        raise WorkerStopError("process group 1 is still running")

    backend.on_stop = denied
    result = check_once(router)[0]
    assert result.action is RepairAction.ESCALATE
    assert len(backend.started) == 1
    assert router.store.get_ticket(result.ticket_id).status is TicketStatus.ENDED
    assert "The watchdog could not stop the worker. It may still run." in router.relay()[0][2]


def test_an_unstoppable_worker_escalates_once(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    report(router, backend, "same", 3)
    check_once(router)
    report(router, backend, "same", 1)

    def denied(assignment_id: str) -> None:
        raise WorkerStopError("process group 1 is still running")

    backend.on_stop = denied
    check_once(router, policy=WatchdogPolicy(reap_grace_seconds=-1))
    check_once(router, policy=WatchdogPolicy(reap_grace_seconds=-1))
    assert len(router.relay()) == 1


def test_reaper_failure_leaves_a_running_replacement_alone(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    ticket = router.store.list_tickets()[0]
    router.start_worker(ticket)

    def denied(assignment_id: str) -> None:
        raise WorkerStopError("process group 1 is still running")

    backend.on_stop = denied
    check_once(router, policy=WatchdogPolicy(reap_grace_seconds=-1))
    assert router.store.get_ticket(ticket.id).status is TicketStatus.OPEN
    assert len(router.store.running_assignments()) == 1
    assert "The watchdog could not stop the worker. It may still run." in router.relay()[0][2]


def test_one_unstoppable_finished_worker_does_not_block_the_checks(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    assignment = router.store.running_assignments()[0]
    router.store.add_report(assignment.id, backend.started[0][2], ReportState.READY, "done")
    router.ingest(Trigger("t2", "chat", "th2", "alice", "another", "chat:th2", True, False))

    def denied(assignment_id: str) -> None:
        raise WorkerStopError("process group 1 is still running")

    backend.on_stop = denied
    results = check_once(router, policy=WatchdogPolicy(reap_grace_seconds=-1))
    assert [item.verdict for item in results] == [Verdict.ON_TRACK]
    assert any("The watchdog could not stop the worker." in text for _, _, text in router.relay())


def test_dead_worker_skips_the_nudge(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    backend.alive = False
    result = check_once(router)[0]
    assert result.action is RepairAction.RESTART
    assert "gone" in result.reason


def test_silent_worker_counts_as_stuck(router: Router) -> None:
    router.ingest(REQUEST)
    result = check_once(router, policy=WatchdogPolicy(stall_after_seconds=-1))[0]
    assert result.verdict is Verdict.STUCK


class DriftingJudge:
    def judge(self, goal: str, reports: tuple[Report, ...]) -> Judgement:
        return Judgement(Verdict.DRIFTING, f"reports do not serve the goal: {goal}")


def test_semantic_drift_triggers_a_repair(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    report(router, backend, "refactoring the billing module")
    result = check_once(router, judge=DriftingJudge())[0]
    assert (result.verdict, result.action) == (Verdict.DRIFTING, RepairAction.NUDGE)


def test_judge_only_runs_on_new_reports(router: Router, backend: FakeBackend) -> None:
    calls: list[str] = []

    class CountingJudge:
        def judge(self, goal: str, reports: tuple[Report, ...]) -> Judgement:
            calls.append(goal)
            return Judgement(Verdict.ON_TRACK, "fine")

    router.ingest(REQUEST)
    report(router, backend, "working")
    check_once(router, judge=CountingJudge())
    check_once(router, judge=CountingJudge())
    assert len(calls) == 1


def test_repeated_identical_tool_calls_count_as_stuck(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    assignment = router.store.running_assignments()[0]
    for _ in range(5):
        router.store.record_activity(assignment.id, backend.started[0][2], "same-call")
    result = check_once(router)[0]
    assert result.verdict is Verdict.STUCK
    assert "same tool call" in result.reason


def test_varied_tool_calls_count_as_alive(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    assignment = router.store.running_assignments()[0]
    for index in range(5):
        router.store.record_activity(assignment.id, backend.started[0][2], f"call-{index}")
    assert check_once(router)[0].verdict is Verdict.ON_TRACK


def test_tool_calls_without_a_report_do_not_keep_a_worker_alive(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    assignment = router.store.running_assignments()[0]
    with router.store._connect() as connection:
        connection.execute("UPDATE assignments SET started_at = ? WHERE id = ?", (time.time() - 1000, assignment.id))
    for index in range(3):
        router.store.record_activity(assignment.id, backend.started[0][2], f"wait-{index}")
    result = check_once(router, policy=WatchdogPolicy(stall_after_seconds=900))[0]
    assert result.verdict is Verdict.STUCK
    assert "no report for" in result.reason


def test_a_recent_report_keeps_a_long_running_worker_on_track(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    assignment = router.store.running_assignments()[0]
    with router.store._connect() as connection:
        connection.execute("UPDATE assignments SET started_at = ? WHERE id = ?", (time.time() - 1000, assignment.id))
    report(router, backend, "implement running, 16 min, 40 kB of output")
    assert check_once(router, policy=WatchdogPolicy(stall_after_seconds=900))[0].verdict is Verdict.ON_TRACK


def test_stale_worker_cannot_record_activity(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    assignment = router.store.running_assignments()[0]
    router.store.end_assignment(assignment.id)
    with pytest.raises(StaleAssignmentError):
        router.store.record_activity(assignment.id, backend.started[0][2], "late")


def test_worker_process_left_running_after_a_final_report_is_stopped(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    assignment = router.store.running_assignments()[0]
    router.store.add_report(assignment.id, backend.started[0][2], ReportState.READY, "done")
    check_once(router, policy=WatchdogPolicy(reap_grace_seconds=3600))
    assert backend.stopped == []
    check_once(router, policy=WatchdogPolicy(reap_grace_seconds=0))
    check_once(router, policy=WatchdogPolicy(reap_grace_seconds=0))
    assert backend.stopped == [assignment.id]


def test_open_ticket_without_a_worker_is_recovered_then_escalated(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    assignment = router.store.running_assignments()[0]
    router.store.end_assignment(assignment.id)
    policy = WatchdogPolicy(orphan_after_seconds=-1)
    assert check_once(router, policy=policy)[0].action is RepairAction.RESTART
    assert len(backend.started) == 2
    router.store.end_assignment(router.store.running_assignments()[0].id)
    escalated = check_once(router, policy=policy)[0]
    assert escalated.action is RepairAction.ESCALATE
    assert router.store.get_ticket(escalated.ticket_id).status is TicketStatus.ENDED
    assert "no running worker" in router.relay()[0][2]


def test_a_recent_ticket_is_not_treated_as_orphaned(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    router.store.end_assignment(router.store.running_assignments()[0].id)
    assert check_once(router, policy=WatchdogPolicy(orphan_after_seconds=3600)) == []


def test_a_worker_still_starting_is_left_alone(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    router.store.end_assignment(router.store.running_assignments()[0].id)
    backend.fail_next_start = True
    ticket = router.store.list_tickets()[0]
    with pytest.raises(OSError):
        router.start_worker(ticket)
    assert check_once(router) == []
    backend.alive = False
    assert check_once(router, policy=WatchdogPolicy(orphan_after_seconds=-1))[0].action is RepairAction.RESTART


def test_repair_budgets_start_over_after_a_person_resumes_the_ticket(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    report(router, backend, "same", 3)
    check_once(router)
    report(router, backend, "same", 1)
    assert check_once(router)[0].action is RepairAction.RESTART
    assignment = router.store.running_assignments()[0]
    router.store.add_report(assignment.id, backend.started[-1][2], ReportState.BLOCKED, "stuck", "which one?")
    router.ingest(Trigger("t2", "chat", "th1", "alice", "the first", "chat:th1", True, False))
    report(router, backend, "again", 3)
    assert check_once(router)[0].action is RepairAction.NUDGE


def test_every_check_is_recorded(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    report(router, backend, "working")
    result = check_once(router)[0]
    assert router.store.watchdog_events(result.ticket_id)[0][1] == "none"


def test_a_worker_that_finishes_during_the_check_is_not_run_again(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)

    def finishes_now(assignment: Assignment) -> None:
        router.store.add_report(assignment.id, backend.started[-1][2], ReportState.READY, "done")

    backend.on_alive_check = finishes_now
    backend.alive = False
    assert check_once(router) == []
    assert len(backend.started) == 1
    assert router.store.running_assignments() == []
    assert router.store.live_tickets() == []


def test_an_ended_ticket_never_gets_a_new_worker(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    ticket = router.store.live_tickets()[0]
    router.store.end_ticket(ticket.id)
    assert router.start_worker(ticket) is None
    assert len(backend.started) == 1


def test_a_worker_that_exits_without_reporting_is_parked_for_a_person(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    backend.alive = False
    backend.output = "I opened a pull request that fixes the login bug."
    result = check_once(router)[0]
    assert result.action is RepairAction.PARK
    assert len(backend.started) == 1
    ticket = router.store.get_ticket(result.ticket_id)
    assert ticket.status is TicketStatus.WAITING
    [message] = [text for _, _, text in router.relay()]
    assert unlabelled(message) == (
        "Needs you: Should the worker run again?\nIt stopped without a final report.\n"
        f'Reply "approve {ticket.id}" or say what to change.'
    )
    assert "opened a pull request" not in message  # raw worker output stays out of chat
    assert "opened a pull request" in router.details(ticket.id)
    assert check_once(router) == []


def test_a_worker_that_dies_silently_is_still_restarted(router: Router, backend: FakeBackend) -> None:
    router.ingest(REQUEST)
    backend.alive = False
    assert check_once(router)[0].action is RepairAction.RESTART
    assert len(backend.started) == 2
