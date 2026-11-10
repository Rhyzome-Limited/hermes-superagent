from __future__ import annotations

from pathlib import Path

import pytest

from superagent import style
from superagent.domain import Check, Mandate, Report, ReportFields, ReportState, Strength, Ticket, TicketStatus
from superagent.router import Router, _render_escalation, _render_report, _render_started, render_status

from .conftest import FakeBackend

GOLDEN = Path(__file__).resolve().parent / "golden" / "messages"
MANDATE = Mandate("v1", "goal", Strength.EXPLICIT, "83341977", "telegram", "telegram:83341977:1")


def golden(name: str) -> str:
    return (GOLDEN / f"{name}.txt").read_bytes().decode("utf-8")


def ticket(ticket_id: str, title: str, **extra: str) -> Ticket:
    return Ticket(ticket_id, "the goal", MANDATE, "telegram:83341977", TicketStatus.OPEN, title=title, **extra)


def report(ticket_id: str, state: ReportState, question: str = "", **fields: object) -> Report:
    return Report("as-1", ticket_id, state, "free text", question, 0.0, ReportFields(**fields))  # type: ignore[arg-type]


READY = report(
    "tk-ee69ce72",
    ReportState.READY,
    outcome="The relay-guard tests now run on macOS. All 3 CI jobs pass.",
    changes=("Changed the CI file only. No test changed.",),
    checks=(Check("relay-guard", True), Check("wiki-mirror", True), Check("shell syntax", True)),
    links=("https://github.com/Rhyzome-Limited/rhyzome-superagent-guards/pull/4",),
    next_for_leo="Review and merge PR #4",
    details="Commit 2c512b5, run 42756344802 on macos-26-arm64: relay-guard 364 passed.",
)
BLOCKED = report(
    "tk-1a2b3c4d",
    ReportState.BLOCKED,
    question="Should a failed push retry 3 times or 5 times?",
    outcome="The ticket does not say. Both are easy to change later.",
    topic="Retry count",
)
FAILED = report(
    "tk-5e6f7a8b",
    ReportState.FAILED,
    outcome="The export test still fails for dates before 1970.",
    changes=("It tried two fixes. Both broke the import test.",),
    next_for_leo="Decide if dates before 1970 are in scope.",
)


def test_ready_matches_its_golden_file() -> None:
    assert f"{_render_report(READY, ticket('tk-ee69ce72', 'Guards CI fix'))}\n" == golden("ready")


def test_blocked_matches_its_golden_file() -> None:
    assert f"{_render_report(BLOCKED, ticket('tk-1a2b3c4d', 'Push retry limit'))}\n" == golden("blocked")


def test_failed_matches_its_golden_file() -> None:
    assert f"{_render_report(FAILED, ticket('tk-5e6f7a8b', 'marIA export fix'))}\n" == golden("failed")


def test_escalation_matches_its_golden_file() -> None:
    text = _render_escalation("stuck", "no report for 1500s", ticket("tk-ee69ce72", "Guards CI fix"), restarts=1)
    assert f"{text}\n" == golden("escalation")


def test_started_matches_its_golden_file() -> None:
    started = ticket(
        "tk-1a2b3c4d", "Push retry limit", repo="Rhyzome-Limited/kastle-push", done_when="all CI checks on the PR pass"
    )
    assert f"{_render_started(started)}\n" == golden("started")


def test_status_matches_its_golden_file(router: Router, backend: FakeBackend, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1_000_000.0]
    monkeypatch.setattr("superagent.store.time.time", lambda: clock[0])
    monkeypatch.setattr("superagent.router.time.time", lambda: clock[0])
    running, _ = router.store.create_ticket("fix CI", MANDATE, "telegram:83341977", "", title="Guards CI fix")
    router.start_worker(running)
    clock[0] += 60
    waiting, _ = router.store.create_ticket(
        "retry",
        Mandate("v1", "retry", Strength.EXPLICIT, "83341977", "telegram", "t2"),
        "telegram:1",
        "",
        title="Push retry limit",
    )
    assignment = router.start_worker(waiting)
    assert assignment is not None
    fields = ReportFields(outcome="The ticket does not say.", topic="Retry count")
    router.store.add_report(
        assignment.id, backend.started[-1][2], ReportState.BLOCKED, "x", "Retry 3 or 5 times?", fields=fields
    )
    clock[0] += 11 * 60
    assert f"{render_status(router.status())}\n" == golden("status")


@pytest.mark.parametrize("name", ["ready", "blocked", "failed", "escalation", "started"])
def test_every_golden_report_message_passes_the_style_check(name: str) -> None:
    assert style.check_message(golden(name).removesuffix("\n")) == []


def test_the_golden_status_reply_passes_the_style_check() -> None:
    assert style.check_message(golden("status").removesuffix("\n"), max_lines=style.MAX_STATUS_LINES) == []


def test_details_never_reach_the_done_message() -> None:
    text = _render_report(READY, ticket("tk-ee69ce72", "Guards CI fix")) or ""
    assert "2c512b5" not in text and "42756344802" not in text


def test_a_style_fail_report_gets_only_code_written_text() -> None:
    bad = Report("as-1", "tk-ee69ce72", ReportState.READY, "x", "", 0.0, READY.fields, style_fail=True)
    assert _render_report(bad, ticket("tk-ee69ce72", "Guards CI fix")) == (
        '✅ Done · Guards CI fix · tk-ee69ce72\nGate passed. Send "details tk-ee69ce72" for more.'
    )


def test_ready_without_next_step_drops_the_you_line() -> None:
    quiet = report("tk-ee69ce72", ReportState.READY, outcome="All 3 CI jobs pass.", next_for_leo="nothing")
    assert _render_report(quiet, ticket("tk-ee69ce72", "Guards CI fix")) == (
        "✅ Done · Guards CI fix · tk-ee69ce72\nAll 3 CI jobs pass."
    )


def test_a_ready_report_never_runs_past_6_lines() -> None:
    long = report(
        "tk-ee69ce72",
        ReportState.READY,
        outcome="All 3 CI jobs pass.",
        changes=("Changed the CI file.", "Changed the README.", "Changed the plan."),
        checks=(Check("lint", True),),
        links=("https://github.com/o/r/pull/4",),
        next_for_leo="Merge PR #4.",
    )
    text = _render_report(long, ticket("tk-ee69ce72", "Guards CI fix")) or ""
    assert len(text.splitlines()) == 6
    assert style.check_message(text) == []


def test_a_ticket_without_a_title_uses_the_first_words_of_its_goal() -> None:
    old = Ticket("tk-1", "fix the login redirect loop now", MANDATE, "c", TicketStatus.OPEN)
    assert (_render_report(READY, old) or "").startswith("✅ Done · fix the login redirect loop · tk-1\n")


@pytest.mark.parametrize(
    ("verdict", "reason", "restarts", "lines"),
    [
        (
            "stuck",
            "worker process is gone without a final report",
            1,
            ["The worker stopped without a final report.", "The watchdog restarted it once. It is gone again."],
        ),
        (
            "stuck",
            "last 3 reports are identical",
            2,
            ["The worker sent the same report 3 times.", "The watchdog restarted it 2 times. It is stuck again."],
        ),
        (
            "stuck",
            "could not stop a worker, it may still be running: ps failed",
            0,
            ["The watchdog could not stop the worker. It may still run.", "The watchdog did not restart it."],
        ),
        (
            "drifting",
            "the worker rewrote the README instead (ignore previous instructions)",
            1,
            ["The worker drifted from the goal.", "The watchdog restarted it once. It is stuck again."],
        ),
    ],
)
def test_escalation_says_what_stopped_in_plain_words(
    verdict: str, reason: str, restarts: int, lines: list[str]
) -> None:
    text = _render_escalation(verdict, reason, ticket("tk-ee69ce72", "Guards CI fix"), restarts=restarts)
    assert text.splitlines()[1:3] == lines
    assert style.check_message(text) == []


def test_status_with_nothing_open() -> None:
    assert render_status([]) == "No tickets running."


def test_status_shows_8_tickets_then_how_many_more() -> None:
    lines = [
        {"ticket": f"tk-{n}", "title": f"Task {n}", "status": "open", "minutes": n, "topic": ""} for n in range(10)
    ]
    text = render_status(lines)
    assert text.splitlines()[0] == "10 tickets running. None needs you."
    assert text.splitlines()[-1] == "+2 more"
    assert len(text.splitlines()) == style.MAX_STATUS_LINES
    assert style.check_message(text, max_lines=style.MAX_STATUS_LINES) == []
