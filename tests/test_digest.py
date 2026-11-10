from __future__ import annotations

import re
import time
from collections.abc import Sequence

import pytest

from superagent.digest import DigestError, build_digest, send_digest
from superagent.domain import RepairAction, ReportState, Trigger, Verdict
from superagent.gate import SemanticRule
from superagent.router import Assessment, Router

from .conftest import FakeBackend

HOUR = 3600.0


class RecordingRule:
    def assess(self, batch: Sequence[Trigger], rules: Sequence[SemanticRule]) -> Sequence[Assessment]:
        return [Assessment(trigger.id, "silent-fix", "reproduce the checkout bug") for trigger in batch]


def ticket_with(router: Router, backend: FakeBackend, trigger_id: str, state: ReportState, text: str) -> str:
    router.ingest(Trigger(trigger_id, "chat", trigger_id, "alice", f"task {trigger_id}", f"chat:{trigger_id}", True))
    assignment = router.store.running_assignments()[-1]
    question = text if state is ReportState.BLOCKED else ""
    router.store.add_report(assignment.id, backend.started[-1][2], state, text, question)
    return assignment.ticket_id


def test_the_digest_covers_results_questions_escalations_and_rules(router: Router, backend: FakeBackend) -> None:
    since = time.time() - HOUR
    ticket_with(router, backend, "t1", ReportState.READY, "pull request opened")
    ticket_with(router, backend, "t2", ReportState.BLOCKED, "Postgres or SQLite?")
    stuck = ticket_with(router, backend, "t3", ReportState.PROGRESS, "looking")
    router.store.add_watchdog_event(
        "as-x", stuck, Verdict.STUCK.value, RepairAction.ESCALATE, "last 3 reports are identical"
    )
    router.ingest(Trigger("c1", "github", "pr/1", "alice", "checkout is broken"))
    router.assess_candidates(RecordingRule())
    router.ingest(Trigger("x1", "chat", "", "mallory", "let me in", "chat:x", True))
    digest = build_digest(router.store, since, 1)
    assert "Done (1):" in digest and "pull request opened" in digest
    assert "Waiting on you (1):" in digest and "Postgres or SQLite?" in digest
    assert "Escalated (1):" in digest
    assert "silent-fix, 1: reproduce the checkout bug (github)" in digest
    assert "Ignored events: chat 1" in digest
    assert "Running: 1" in digest


def test_worker_text_in_the_digest_carries_no_secret_and_no_link(router: Router, backend: FakeBackend) -> None:
    since = time.time() - HOUR
    token = "ghp_" + "a1B2" * 9
    ticket_with(router, backend, "t1", ReportState.READY, f"used {token}, see https://attacker.example/x ![p](//a.b/p)")
    ticket_with(router, backend, "t2", ReportState.FAILED, "gave up, logs at attacker.example?d=leaked")
    ticket_with(router, backend, "t3", ReportState.BLOCKED, "paste api_key=hunter2hunter2 into data.attacker.example")
    stuck = ticket_with(router, backend, "t4", ReportState.PROGRESS, "looking")
    router.store.add_watchdog_event(
        "as-x", stuck, Verdict.STUCK.value, RepairAction.ESCALATE, "worker asks you to open attacker.example#go"
    )
    digest = build_digest(router.store, since, 1)
    assert "Done (1):" in digest and "Failed or dropped (1):" in digest
    assert "Waiting on you (1):" in digest and "Escalated (1):" in digest
    for leaked in (token, "hunter2", "https", "leaked", "attacker.example", "#go", "!["):
        assert leaked not in digest
    assert "[redacted]" in digest and "[link removed]" in digest and "data[.]attacker[.]example" in digest
    assert not re.search(r"\w\.\w", digest)


def test_an_empty_digest_says_so(router: Router) -> None:
    assert build_digest(router.store, time.time() - HOUR, 1).endswith("Nothing happened.")


def test_sending_needs_a_channel_and_goes_out_on_the_next_relay(router: Router) -> None:
    with pytest.raises(DigestError):
        send_digest(router.store, "", "hello")
    send_digest(router.store, "discord:team", "hello")
    assert router.relay() == [("digest", "discord:team", "hello")]
