from __future__ import annotations

import json
import secrets
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from superagent.config import DB_FILENAME, WORKDIRS_DIRNAME
from superagent.domain import Mandate, ReportState, Strength
from superagent.store import Store
from superagent.worker import write_identity

from .test_plugin import FakeContext, load_plugin_module

ROOT = Path(__file__).resolve().parent.parent


def ticket_with(store: Store, loop: str) -> tuple[str, str, str]:
    mandate = Mandate("v", "g", Strength.EXPLICIT, "alice", "chat", secrets.token_hex(4))
    ticket, _ = store.create_ticket("g", mandate, "chat:1", "", loop)
    created = store.create_assignment(ticket.id)
    assert created is not None
    assignment, token = created
    return ticket.id, assignment.id, token


def test_a_kastle_ticket_stores_its_scope_class(store: Store) -> None:
    ticket_id, assignment_id, token = ticket_with(store, "kastle-build-loop")
    store.add_report(assignment_id, token, ReportState.PROGRESS, "scope triage done", scope_class="in-scope")
    assert store.get_ticket(ticket_id).scope_class == "in-scope"
    store.add_report(assignment_id, token, ReportState.PROGRESS, "touches transaction logic", scope_class="leo-gated")
    assert store.get_ticket(ticket_id).scope_class == "leo-gated"


def test_leo_gated_never_changes_back(store: Store) -> None:
    ticket_id, assignment_id, token = ticket_with(store, "kastle-build-loop")
    store.add_report(assignment_id, token, ReportState.PROGRESS, "release work", scope_class="leo-gated")
    with pytest.raises(ValueError, match="never changes"):
        store.add_report(assignment_id, token, ReportState.PROGRESS, "actually fine", scope_class="in-scope")
    assert store.get_ticket(ticket_id).scope_class == "leo-gated"
    assert [report.summary for report in store.ticket_reports(ticket_id)] == ["release work"]
    store.add_report(assignment_id, token, ReportState.PROGRESS, "still gated", scope_class="leo-gated")
    store.add_report(assignment_id, token, ReportState.PROGRESS, "no class given")
    assert store.get_ticket(ticket_id).scope_class == "leo-gated"


def test_leo_gated_survives_a_new_attempt(store: Store) -> None:
    ticket_id, assignment_id, token = ticket_with(store, "kastle-build-loop")
    store.add_report(assignment_id, token, ReportState.BLOCKED, "public copy", "Approve the text?", "leo-gated")
    resumed = store.resume_waiting_ticket(ticket_id)
    assert resumed is not None
    second, second_token = resumed
    with pytest.raises(ValueError, match="never changes"):
        store.add_report(second.id, second_token, ReportState.PROGRESS, "fresh start", scope_class="in-scope")
    assert store.get_ticket(ticket_id).scope_class == "leo-gated"


@pytest.mark.parametrize("loop", ["claude-assisted-loop", "build-loop", ""])
def test_only_kastle_build_loop_tickets_take_a_scope_class(store: Store, loop: str) -> None:
    ticket_id, assignment_id, token = ticket_with(store, loop)
    with pytest.raises(ValueError, match="only accepted on kastle-build-loop"):
        store.add_report(assignment_id, token, ReportState.PROGRESS, "triage", scope_class="in-scope")
    assert store.get_ticket(ticket_id).scope_class == ""
    assert store.ticket_reports(ticket_id) == []


@pytest.mark.parametrize("value", ["In-Scope", "gated", "in scope", "leo_gated"])
def test_an_unknown_scope_class_is_refused(store: Store, value: str) -> None:
    ticket_id, assignment_id, token = ticket_with(store, "kastle-build-loop")
    with pytest.raises(ValueError, match="scope_class must be"):
        store.add_report(assignment_id, token, ReportState.PROGRESS, "triage", scope_class=value)
    assert store.get_ticket(ticket_id).scope_class == ""


def test_the_column_is_what_the_relay_guard_reads(store: Store) -> None:
    ticket_id, assignment_id, token = ticket_with(store, "kastle-build-loop")
    store.add_report(assignment_id, token, ReportState.PROGRESS, "triage", scope_class="in-scope")
    with sqlite3.connect(f"file:{store.path}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT loop, scope_class FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    assert row == ("kastle-build-loop", "in-scope")


def worker_context(home: Path, loop: str) -> tuple[FakeContext, Store, str]:
    (home / "superagent.toml").write_text('[sources.chat]\npolicy = "explicit_only"\n', encoding="utf-8")
    store = Store(home / DB_FILENAME)
    ticket_id, assignment_id, token = ticket_with(store, loop)
    workdir = home / WORKDIRS_DIRNAME / ticket_id / assignment_id
    workdir.mkdir(parents=True)
    write_identity(workdir, ticket_id, assignment_id, token, 1, loop, str(home))
    ctx = FakeContext({"role": "worker", "home": str(home)}, cwd=str(workdir))
    load_plugin_module().register(ctx)
    return ctx, store, ticket_id


def test_report_tool_takes_a_scope_class(tmp_path: Path) -> None:
    ctx, store, ticket_id = worker_context(tmp_path, "kastle-build-loop")
    schema = ctx.tools["superagent_report"]["schema"]["parameters"]
    assert schema["properties"]["scope_class"]["enum"] == ["in-scope", "leo-gated"]
    assert "scope_class" not in schema["required"]
    report = ctx.tools["superagent_report"]["handler"]
    answer = json.loads(report({"state": "progress", "summary": "triaged", "scope_class": "leo-gated"}))
    assert answer["scope_class"] == "leo-gated"
    refused = json.loads(report({"state": "progress", "summary": "retriaged", "scope_class": "in-scope"}))
    assert "never changes" in refused["error"]
    assert store.get_ticket(ticket_id).scope_class == "leo-gated"


def test_report_tool_refuses_a_scope_class_outside_kastle_build_loop(tmp_path: Path) -> None:
    ctx, store, ticket_id = worker_context(tmp_path, "claude-assisted-loop")
    answer = json.loads(
        ctx.tools["superagent_report"]["handler"]({"state": "progress", "summary": "x", "scope_class": "in-scope"})
    )
    assert "only accepted on kastle-build-loop" in answer["error"]
    assert store.get_ticket(ticket_id).scope_class == ""


def test_cli_report_takes_a_scope_class(tmp_path: Path) -> None:
    store = Store(tmp_path / DB_FILENAME)
    ticket_id, assignment_id, token = ticket_with(store, "kastle-build-loop")
    workdir = tmp_path / WORKDIRS_DIRNAME / ticket_id / assignment_id
    workdir.mkdir(parents=True)
    write_identity(workdir, ticket_id, assignment_id, token, 1, "kastle-build-loop", str(tmp_path))
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "superagent",
            "report",
            "--state",
            "progress",
            "--summary",
            "s",
            "--scope-class",
            "in-scope",
        ],
        cwd=workdir,
        env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["scope_class"] == "in-scope"
    assert store.get_ticket(ticket_id).scope_class == "in-scope"
