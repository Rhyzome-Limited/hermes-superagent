from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from superagent.config import ConfigError, parse_config
from superagent.domain import ReportState, TicketStatus, Trigger
from superagent.gate import Rules
from superagent.router import Outcome, Router
from superagent.runtime import Runtime, open_runtime
from superagent.store import Store
from superagent.watchdog import check_once

from .conftest import FakeBackend

ROOT = Path(__file__).resolve().parent.parent


def request(router: Router, n: int, text: str = "") -> str:
    """One person asks, the gate admits it, the router dispatches it. Returns the ticket id."""
    words = text or f"task number {n}"
    trigger = Trigger(f"t{n}", "chat", "", "alice", words, "chat:dm", True)
    assert router.ingest(trigger, auto_dispatch=False).outcome is Outcome.ADMITTED
    router.store.take_admitted("chat", "alice", words, 600)
    return router.dispatch(f"t{n}", words, "build-loop").id


def running(router: Router) -> int:
    return len(router.store.running_assignments())


def statuses(router: Router, tickets: list[str]) -> list[str]:
    return [router.store.get_ticket(ticket).status.value for ticket in tickets]


def finish(router: Router, backend: FakeBackend, ticket_id: str) -> None:
    [(assignment_id, token)] = [(a, t) for ticket, a, t in backend.started if ticket == ticket_id]
    router.store.add_report(assignment_id, token, ReportState.READY, f"{ticket_id} done")


@pytest.fixture
def capped(store: Store, rules: Rules, backend: FakeBackend) -> Router:
    return Router(store, rules, backend, max_running=4)


def test_max_running_defaults_to_four_and_is_validated() -> None:
    assert parse_config("").max_running == 4
    assert parse_config('[worker]\nargv = ["w", "{brief}"]\nmax_running = 2\n').max_running == 2
    for bad in ("0", "-1", '"4"', "true", "1.5"):
        with pytest.raises(ConfigError, match="max_running"):
            parse_config(f'[worker]\nargv = ["w", "{{brief}}"]\nmax_running = {bad}\n')


def test_six_requests_run_four_and_queue_two_until_slots_free(capped: Router, backend: FakeBackend) -> None:
    tickets = [request(capped, n) for n in range(1, 7)]
    assert running(capped) == 4
    assert len(backend.started) == 4
    assert statuses(capped, tickets) == ["open"] * 4 + ["queued"] * 2
    assert [line["status"] for line in capped.status()] == ["open"] * 4 + ["queued"] * 2
    finish(capped, backend, tickets[2])
    assert running(capped) == 3
    check_once(capped)
    assert running(capped) == 4
    assert [ticket for ticket, _, _ in backend.started][4] == tickets[4]
    assert statuses(capped, tickets)[5] == "queued"
    for ticket in tickets:
        if capped.store.get_ticket(ticket).status is TicketStatus.OPEN:
            finish(capped, backend, ticket)
        check_once(capped)
        assert running(capped) <= 4
    for ticket in tickets:
        if capped.store.get_ticket(ticket).status is TicketStatus.OPEN:
            finish(capped, backend, ticket)
    assert statuses(capped, tickets) == ["ended"] * 6
    assert all(capped.store.ticket_reports(ticket)[-1].state is ReportState.READY for ticket in tickets)
    assert [ticket for ticket, _, _ in backend.started] == tickets


def test_dispatch_over_the_cap_answers_queued(capped: Router) -> None:
    for n in range(1, 5):
        request(capped, n)
    trigger = Trigger("t5", "chat", "", "alice", "one more", "chat:dm", True)
    capped.ingest(trigger, auto_dispatch=False)
    capped.store.take_admitted("chat", "alice", "one more", 600)
    assert capped.dispatch("t5", "one more", "build-loop").status is TicketStatus.QUEUED


def test_queue_is_first_in_first_out(store: Store, rules: Rules, backend: FakeBackend) -> None:
    router = Router(store, rules, backend, max_running=1)
    first, second, third = (request(router, n) for n in range(1, 4))
    finish(router, backend, first)
    router.start_queued()
    finish(router, backend, second)
    router.start_queued()
    assert [ticket for ticket, _, _ in backend.started] == [first, second, third]


def test_a_cancel_frees_its_slot_at_once(capped: Router, backend: FakeBackend) -> None:
    tickets = [request(capped, n) for n in range(1, 6)]
    capped.cancel(tickets[0], "alice")
    assert capped.store.get_ticket(tickets[4]).status is TicketStatus.OPEN
    assert running(capped) == 4


def test_a_cancelled_queued_ticket_never_starts(capped: Router, backend: FakeBackend) -> None:
    tickets = [request(capped, n) for n in range(1, 6)]
    capped.cancel(tickets[4], "alice")
    finish(capped, backend, tickets[0])
    check_once(capped)
    assert tickets[4] not in [ticket for ticket, _, _ in backend.started]


def test_a_resumed_ticket_over_the_cap_waits_in_the_queue_with_its_answer(
    store: Store, rules: Rules, backend: FakeBackend
) -> None:
    router = Router(store, rules, backend, max_running=1)
    parked = request(router, 1)
    [(_, assignment_id, token)] = backend.started
    store.add_report(assignment_id, token, ReportState.BLOCKED, "need a decision", "Postgres or SQLite?")
    busy = request(router, 2)
    router.ingest(Trigger("t3", "chat", "", "alice", "use SQLite", "chat:dm", True), auto_dispatch=False)
    store.take_admitted("chat", "alice", "use SQLite", 600)
    assert router.forward("t3", parked).status is TicketStatus.QUEUED
    assert len(backend.started) == 2
    finish(router, backend, busy)
    router.start_queued()
    assert backend.started[-1][0] == parked
    assert "use SQLite" in backend.contexts[-1]


def test_the_cap_holds_across_two_processes_sharing_the_store(store: Store, rules: Rules) -> None:
    one, two = FakeBackend(), FakeBackend()
    routers = [Router(store, rules, one, max_running=4), Router(store, rules, two, max_running=4)]
    errors: list[BaseException] = []

    def dispatch_three(router: Router, offset: int) -> None:
        try:
            for n in range(offset, offset + 3):
                request(router, n)
        except BaseException as error:  # pragma: no cover - surfaced below
            errors.append(error)

    threads = [threading.Thread(target=dispatch_three, args=(router, 1 + 3 * i)) for i, router in enumerate(routers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(store.running_assignments()) == 4
    assert len(one.started) + len(two.started) == 4
    assert len(store.list_tickets(TicketStatus.QUEUED)) == 2


WORKER = """
import sys, time
from pathlib import Path
sys.path.insert(0, {root!r})
from superagent.domain import ReportState
from superagent.reporting import identity_from_workdir, submit_report
identity = identity_from_workdir(Path.cwd())
home = identity.db_path.parent
submit_report(identity, ReportState.PROGRESS, "started")
deadline = time.time() + 90
while not (home / "release").exists() and time.time() < deadline:
    time.sleep(0.05)
submit_report(identity, ReportState.READY, "finished " + identity.ticket_id)
"""


def scripted_home(tmp_path: Path) -> Runtime:
    script = tmp_path / "worker.py"
    script.write_text(WORKER.format(root=str(ROOT)), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    argv = json.dumps([sys.executable, str(script), "{brief}"])
    (home / "superagent.toml").write_text(
        f'[sources.chat]\npolicy = "explicit_only"\nallow = ["alice"]\n\n[worker]\nargv = {argv}\nmax_running = 4\n',
        encoding="utf-8",
    )
    return open_runtime(home)


def wait_watching(runtime: Runtime, done: Callable[[], bool], timeout: float = 60) -> int:
    """Waits for done(), sampling the number of running assignments. Returns the most seen at once."""
    most = 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        most = max(most, len(runtime.store.running_assignments()))
        if done():
            return most
        time.sleep(0.02)
    raise AssertionError("timed out")


def all_ready(runtime: Runtime, tickets: list[str]) -> bool:
    return all(
        any(report.state is ReportState.READY for report in runtime.store.ticket_reports(ticket)) for ticket in tickets
    )


def test_six_real_workers_in_a_burst_run_four_at_a_time_and_all_finish(tmp_path: Path) -> None:
    runtime = scripted_home(tmp_path)
    started = time.time()
    tickets = [request(runtime.router, n) for n in range(1, 7)]
    assert time.time() - started < 120
    assert statuses(runtime.router, tickets) == ["open"] * 4 + ["queued"] * 2
    live = [a for a in runtime.store.running_assignments() if runtime.router.backend.is_alive(a)]
    assert len(live) == 4
    (runtime.home / "release").touch()
    most = wait_watching(runtime, lambda: all_ready(runtime, tickets))
    assert most <= 4
    assert statuses(runtime.router, tickets) == ["ended"] * 6
    summaries = {runtime.store.ticket_reports(ticket)[-1].summary for ticket in tickets}
    assert summaries == {f"finished {ticket}" for ticket in tickets}


def test_queued_tickets_also_start_on_the_tick(tmp_path: Path) -> None:
    runtime = scripted_home(tmp_path)
    runtime.router.backend.on_exit = None  # type: ignore[attr-defined]
    tickets = [request(runtime.router, n) for n in range(1, 7)]
    (runtime.home / "release").touch()
    wait_watching(runtime, lambda: all_ready(runtime, tickets[:4]))
    time.sleep(0.5)
    assert statuses(runtime.router, tickets)[4:] == ["queued", "queued"]
    check_once(runtime.router)
    most = wait_watching(runtime, lambda: all_ready(runtime, tickets))
    assert most <= 4
    assert statuses(runtime.router, tickets) == ["ended"] * 6
