from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from superagent.domain import Assignment, AssignmentStatus, Mandate, Strength, Ticket, TicketStatus
from superagent.reporting import identity_from_workdir
from superagent.worker import (
    IDENTITY_FILENAME,
    SubprocessBackend,
    WorkerStopError,
    _group_has_live_member,
    _Process,
    _process_tree,
    write_identity,
)

STUBBORN = (
    "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nopen('ready', 'w').close()\ntime.sleep(60)\n"
)
PRINT_ENV = "import json, os; print(json.dumps(dict(os.environ)))"


def ticket(goal: str = "a task") -> Ticket:
    mandate = Mandate("v1", goal, Strength.EXPLICIT, "alice", "chat", "t1")
    return Ticket("tk-1", goal, mandate, "chat:1", TicketStatus.OPEN)


def assignment(pid: int | None = None) -> Assignment:
    return Assignment("as-1", "tk-1", 1, AssignmentStatus.RUNNING, pid, time.time())


def test_stop_kills_a_worker_that_ignores_sigterm(tmp_path: Path) -> None:
    script = tmp_path / "stubborn.py"
    script.write_text(STUBBORN, encoding="utf-8")
    backend = SubprocessBackend([sys.executable, str(script), "{brief}"], tmp_path / "w")
    started = assignment()
    pid = backend.start(ticket(), started, "token")
    running = Assignment(started.id, started.ticket_id, 1, started.status, pid, started.started_at)
    ready = tmp_path / "w" / "tk-1" / "as-1" / "ready"
    deadline = time.time() + 10
    while not ready.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert ready.exists()
    assert backend.is_alive(running)
    backend.stop(running)
    deadline = time.time() + 5
    while backend.is_alive(running) and time.time() < deadline:
        time.sleep(0.05)
    assert not backend.is_alive(running)


def test_stop_tolerates_a_worker_that_already_exited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("superagent.worker._STOP_GRACE_SECONDS", 0.2)
    started_at = time.time()
    zombie = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    deadline = time.time() + 10
    while (
        "Z" not in subprocess.run(["ps", "-o", "stat=", "-p", str(zombie.pid)], capture_output=True, text=True).stdout
    ):
        assert time.time() < deadline
        time.sleep(0.05)
    backend = SubprocessBackend(["worker", "{brief}"], tmp_path / "w")
    exited = Assignment("as-1", "tk-1", 1, AssignmentStatus.RUNNING, zombie.pid, started_at)
    try:
        backend.stop(exited)
    finally:
        zombie.wait()


def test_stop_reports_a_group_it_is_not_allowed_to_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(group: int, signum: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    started_at = time.time()
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        monkeypatch.setattr("superagent.worker.os.killpg", denied)
        backend = SubprocessBackend(["worker", "{brief}"], tmp_path / "w")
        running = Assignment("as-1", "tk-1", 1, AssignmentStatus.RUNNING, live.pid, started_at)
        with pytest.raises(WorkerStopError, match="still running"):
            backend.stop(running)
    finally:
        monkeypatch.undo()
        live.kill()
        live.wait()


def test_stop_kills_a_child_that_outlives_its_leader(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    started_at = time.time()
    leader = subprocess.Popen(
        ["/bin/sh", "-c", f"sleep 60 >/dev/null 2>&1 & echo $! > {pid_file}; exit 0"], start_new_session=True
    )
    leader.wait()
    child = int(pid_file.read_text())
    backend = SubprocessBackend(["worker", "{brief}"], tmp_path / "w")
    backend.stop(Assignment("as-1", "tk-1", 1, AssignmentStatus.RUNNING, leader.pid, started_at))
    deadline = time.time() + 5
    while time.time() < deadline:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(child)], capture_output=True, text=True).stdout.strip()
        if not state or "Z" in state:
            return
        time.sleep(0.05)
    os.kill(child, 9)
    pytest.fail("child survived stop")


def test_stop_refuses_a_leaderless_group_too_old_to_identify(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    leader = subprocess.Popen(
        ["/bin/sh", "-c", f"sleep 60 >/dev/null 2>&1 & echo $! > {pid_file}; exit 0"], start_new_session=True
    )
    leader.wait()
    child = int(pid_file.read_text())
    backend = SubprocessBackend(["worker", "{brief}"], tmp_path / "w")
    stale = Assignment("as-1", "tk-1", 1, AssignmentStatus.RUNNING, leader.pid, time.time() - 86400)
    try:
        with pytest.raises(WorkerStopError, match="too old"):
            backend.stop(stale)
        os.kill(child, 0)
    finally:
        os.kill(child, 9)


def test_stop_refuses_when_it_cannot_read_a_live_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    monkeypatch.setattr("superagent.worker._process_started_at", lambda pid: None)
    backend = SubprocessBackend(["worker", "{brief}"], tmp_path / "w")
    try:
        with pytest.raises(WorkerStopError, match="could not read"):
            backend.stop(Assignment("as-1", "tk-1", 1, AssignmentStatus.RUNNING, live.pid, time.time()))
        assert live.poll() is None
    finally:
        live.kill()
        live.wait()


def test_stop_reports_a_group_that_survives_sigkill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[int] = []
    monkeypatch.setattr("superagent.worker.os.killpg", lambda group, signum: sent.append(signum))
    monkeypatch.setattr("superagent.worker._process_table", dict)
    monkeypatch.setattr("superagent.worker._group_has_live_member", lambda group: True)
    monkeypatch.setattr("superagent.worker._process_started_at", lambda pid: 1000.0)
    monkeypatch.setattr("superagent.worker._STOP_GRACE_SECONDS", 0.0)
    monkeypatch.setattr("superagent.worker._KILL_GRACE_SECONDS", 0.0)
    backend = SubprocessBackend(["worker", "{brief}"], tmp_path / "w")
    with pytest.raises(WorkerStopError, match="survived"):
        backend.stop(Assignment("as-1", "tk-1", 1, AssignmentStatus.RUNNING, 4242, 1000.0))
    assert len(sent) == 2


def test_stop_leaves_a_reused_pid_alone(tmp_path: Path) -> None:
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        backend = SubprocessBackend(["worker", "{brief}"], tmp_path / "w")
        backend.stop(Assignment("as-1", "tk-1", 1, AssignmentStatus.RUNNING, other.pid, time.time() - 3600))
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_worker_env_is_an_allowlist(tmp_path: Path) -> None:
    os.environ["SUPERAGENT_TEST_SECRET"] = "hunter2"
    os.environ["HERMES_YOLO_MODE"] = "1"
    try:
        script = tmp_path / "env.py"
        script.write_text(PRINT_ENV, encoding="utf-8")
        backend = SubprocessBackend([sys.executable, str(script), "{brief}"], tmp_path / "w")
        backend.start(ticket(), assignment(), "token")
        log = tmp_path / "w" / "tk-1" / "as-1" / "worker.log"
        deadline = time.time() + 10
        while (not log.exists() or not log.read_text()) and time.time() < deadline:
            time.sleep(0.05)
        output = log.read_text()
    finally:
        del os.environ["SUPERAGENT_TEST_SECRET"]
        del os.environ["HERMES_YOLO_MODE"]
    assert "hunter2" not in output
    assert "HERMES_YOLO_MODE" not in output
    assert "SUPERAGENT_" not in output
    assert "token" not in output


def test_goal_never_reaches_argv(tmp_path: Path) -> None:
    script = tmp_path / "argv.py"
    script.write_text("import sys; print(sys.argv)", encoding="utf-8")
    backend = SubprocessBackend([sys.executable, str(script), "{brief}"], tmp_path / "w")
    backend.start(ticket("--dangerous-flag; rm -rf /"), assignment(), "token")
    log = tmp_path / "w" / "tk-1" / "as-1" / "worker.log"
    deadline = time.time() + 10
    while (not log.exists() or not log.read_text()) and time.time() < deadline:
        time.sleep(0.05)
    assert "dangerous" not in log.read_text()
    assert "--dangerous-flag" in (tmp_path / "w" / "tk-1" / "as-1" / "brief.md").read_text()


def test_identity_is_written_for_the_worker_only(tmp_path: Path) -> None:
    write_identity(tmp_path, "tk-1", "as-1", "secret", 2, "build-loop", "/sa/home")
    identity = tmp_path / IDENTITY_FILENAME
    assert stat.S_IMODE(identity.stat().st_mode) == 0o600
    assert json.loads(identity.read_text()) == {
        "assignment": "as-1",
        "ticket": "tk-1",
        "token": "secret",
        "attempt": 2,
        "loop": "build-loop",
        "home": "/sa/home",
    }


def test_identity_names_the_loop_and_the_absolute_shared_home(tmp_path: Path) -> None:
    home = tmp_path / "sa-home"
    backend = SubprocessBackend([sys.executable, "-c", "pass", "{brief}"], home / "workers")
    mandate = Mandate("v1", "a task", Strength.EXPLICIT, "alice", "chat", "t1")
    kastle = Ticket("tk-1", "a task", mandate, "chat:1", TicketStatus.OPEN, "kastle-build-loop")
    backend.start(kastle, assignment(), "token")
    workdir = home / "workers" / "tk-1" / "as-1"
    identity = json.loads((workdir / IDENTITY_FILENAME).read_text())
    assert identity["loop"] == "kastle-build-loop"
    assert identity["home"] == str(home.resolve())
    assert Path(identity["home"]).is_absolute()
    assert identity_from_workdir(workdir).db_path.parent.resolve() == Path(identity["home"])


def test_last_output_is_the_tail_of_the_worker_log_without_terminal_codes(tmp_path: Path) -> None:
    backend = SubprocessBackend(["worker", "{brief}"], tmp_path / "w")
    log = tmp_path / "w" / "tk-1" / "as-1" / "worker.log"
    log.parent.mkdir(parents=True)
    log.write_text("noise\n" * 2000 + "\x1b[1mFinal answer: done\x1b[0m\n", encoding="utf-8")
    output = backend.last_output(assignment())
    assert output.endswith("Final answer: done")
    assert "\x1b" not in output
    assert len(output) <= 4000
    assert SubprocessBackend(["worker"], tmp_path / "none").last_output(assignment()) == ""


TREE = """
import os, signal, subprocess, sys, time
role, pid_file, stubborn = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
if role == "leader":
    subprocess.Popen([sys.executable, __file__, "child", pid_file, sys.argv[3]])
    if sys.argv[3] == "leaderless":
        while not os.path.exists(pid_file):
            time.sleep(0.05)
        sys.exit(0)
elif role == "child":
    subprocess.Popen([sys.executable, __file__, "grandchild", pid_file, sys.argv[3]])
else:
    os.setsid()
    if stubborn:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        def on_term(signum, frame):
            open(pid_file + ".term", "w").close()
            sys.exit(0)
        signal.signal(signal.SIGTERM, on_term)
    with open(pid_file + ".tmp", "w") as out:
        out.write(str(os.getpid()))
    os.rename(pid_file + ".tmp", pid_file)
time.sleep(60)
"""


def gone(pid: int, within: float = 5.0) -> bool:
    deadline = time.time() + within
    while True:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if not state or "Z" in state:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.05)


def start_tree(tmp_path: Path, stubborn: bool, leaderless: bool = False) -> tuple[SubprocessBackend, Assignment, int]:
    script = tmp_path / "tree.py"
    script.write_text(TREE, encoding="utf-8")
    pid_file = tmp_path / "grandchild.pid"
    mode = "leaderless" if leaderless else "1" if stubborn else "0"
    argv = [sys.executable, str(script), "leader", str(pid_file), mode, "{brief}"]
    backend = SubprocessBackend(argv, tmp_path / "w")
    started = assignment()
    pid = backend.start(ticket(), started, "token")
    deadline = time.time() + 10
    while not pid_file.exists() and time.time() < deadline:
        time.sleep(0.05)
    grandchild = int(pid_file.read_text())
    # The worker leads its own session and group, so its pid is both ids, also once it has exited.
    assert os.getsid(grandchild) != pid
    assert os.getpgid(grandchild) != pid
    return backend, Assignment(started.id, started.ticket_id, 1, started.status, pid, started.started_at), grandchild


def test_stop_kills_a_grandchild_that_left_the_session_with_setsid(tmp_path: Path) -> None:
    backend, running, grandchild = start_tree(tmp_path, stubborn=False)
    try:
        backend.stop(running)
        assert gone(running.pid or 0)
        assert gone(grandchild)
        assert (tmp_path / "grandchild.pid.term").exists(), "the grandchild got SIGKILL without a SIGTERM first"
    finally:
        if not gone(grandchild, within=0):
            os.kill(grandchild, 9)


def test_stop_kills_a_setsid_grandchild_that_ignores_sigterm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("superagent.worker._STOP_GRACE_SECONDS", 0.5)
    backend, running, grandchild = start_tree(tmp_path, stubborn=True)
    try:
        backend.stop(running)
        assert gone(grandchild)
    finally:
        if not gone(grandchild, within=0):
            os.kill(grandchild, 9)


def test_stop_kills_a_setsid_grandchild_after_the_leader_has_exited(tmp_path: Path) -> None:
    # Hermes crashed or ended its turn: the leader is gone, its child lives on in the worker's group, and that child's
    # own child left with setsid. A cancel or the watchdog's reap must still reach the grandchild.
    backend, running, grandchild = start_tree(tmp_path, stubborn=False, leaderless=True)
    group = running.pid or 0
    try:
        assert gone(group)
        assert _group_has_live_member(group)
        backend.stop(running)
        assert gone(grandchild)
        assert (tmp_path / "grandchild.pid.term").exists(), "the grandchild got SIGKILL without a SIGTERM first"
        assert not _group_has_live_member(group)
    finally:
        if not gone(grandchild, within=0):
            os.kill(grandchild, 9)
        if _group_has_live_member(group):
            os.killpg(group, 9)


def test_stop_leaves_unrelated_processes_alone(tmp_path: Path) -> None:
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        backend, running, grandchild = start_tree(tmp_path, stubborn=False)
        backend.stop(running)
        assert gone(grandchild)
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


def test_the_tree_follows_children_and_group_mates_and_spares_the_caller() -> None:
    me, parent = os.getpid(), os.getppid()
    table = {
        parent: _Process(parent, 1, parent, True, "p"),
        me: _Process(me, parent, os.getpgrp(), True, "me"),
        100: _Process(100, 1, 100, True, "leader"),
        101: _Process(101, 100, 101, True, "terminal command in a new session"),
        102: _Process(102, 101, 102, True, "grandchild after setsid"),
        103: _Process(103, 1, 101, True, "orphan left in the command's group"),
        104: _Process(104, 100, 100, False, "zombie"),
        105: _Process(105, 100, os.getpgrp(), True, "child sharing the caller's group"),
        200: _Process(200, 1, 200, True, "unrelated"),
        201: _Process(201, 105, os.getpgrp(), True, "grandchild in the caller's group"),
    }
    tree = _process_tree([100], table)
    assert set(tree) == {100, 101, 102, 103, 105, 201}
    assert tree[102] == "grandchild after setsid"
    assert _process_tree([me], table) == {}
    assert _process_tree([me], table, groups=[os.getpgrp()]) == {}


def test_the_tree_starts_from_the_group_when_the_leader_has_exited() -> None:
    table = {
        110: _Process(110, 1, 100, True, "child left in the exited leader's group"),
        111: _Process(111, 110, 111, True, "grandchild after setsid"),
        112: _Process(112, 111, 111, True, "its child"),
        200: _Process(200, 1, 200, True, "unrelated"),
    }
    assert _process_tree([100], table) == {}
    assert set(_process_tree([100], table, groups=[100])) == {110, 111, 112}


FAKE_GATEWAY = (
    "import subprocess, sys, time\n"
    "for cwd in sys.argv[1].split(','):\n"
    "    p = subprocess.Popen(['sleep', '60'], cwd=cwd, start_new_session=True)\n"
    "    print(p.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def _alive(pid: int) -> bool:
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and "Z" not in state


def _gateway_with_commands_in(*dirs: Path) -> tuple[subprocess.Popen[str], list[int]]:
    """A process that looks like a Hermes gateway in ps, with one detached command in each of dirs."""
    for path in dirs:
        path.mkdir(parents=True, exist_ok=True)
    gateway = subprocess.Popen(
        [sys.executable, "-c", FAKE_GATEWAY, ",".join(map(str, dirs)), "hermes_cli.main", "gateway", "run"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert gateway.stdout is not None
    return gateway, [int(gateway.stdout.readline()) for _ in dirs]


def _finish(gateway: subprocess.Popen[str], pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    gateway.kill()
    gateway.wait()


def test_stop_kills_the_commands_a_shared_gateway_runs_for_the_worker(tmp_path: Path) -> None:
    workers = tmp_path / "workers"
    gateway, (in_workdir, in_tree, other_ticket) = _gateway_with_commands_in(
        workers / "tk-1" / "as-1", tmp_path / "trees" / "tk-1" / "src", tmp_path / "trees" / "tk-10"
    )
    leos_shell = subprocess.Popen(["sleep", "60"], cwd=tmp_path / "trees" / "tk-1", start_new_session=True)
    try:
        SubprocessBackend(["worker", "{brief}"], workers).stop(
            Assignment("as-1", "tk-1", 1, AssignmentStatus.RUNNING, None, time.time())
        )
        deadline = time.time() + 5
        while (_alive(in_workdir) or _alive(in_tree)) and time.time() < deadline:
            time.sleep(0.05)
        assert not _alive(in_workdir) and not _alive(in_tree)
        assert _alive(other_ticket), "a command of another ticket (tk-10) was stopped"
        assert leos_shell.poll() is None, "a process the gateway did not start was stopped"
        assert gateway.poll() is None, "the shared gateway itself was stopped"
    finally:
        leos_shell.kill()
        _finish(gateway, [in_workdir, in_tree, other_ticket])


def test_stop_leaves_the_tree_to_a_later_attempt(tmp_path: Path) -> None:
    workers = tmp_path / "workers"
    (workers / "tk-1" / "as-2").mkdir(parents=True)
    write_identity(workers / "tk-1" / "as-2", "tk-1", "as-2", "token", 2, "claude-assisted-loop", str(tmp_path))
    gateway, (in_old_workdir, in_tree) = _gateway_with_commands_in(
        workers / "tk-1" / "as-1", tmp_path / "trees" / "tk-1"
    )
    try:
        SubprocessBackend(["worker", "{brief}"], workers).stop(
            Assignment("as-1", "tk-1", 1, AssignmentStatus.ENDED, None, time.time())
        )
        deadline = time.time() + 5
        while _alive(in_old_workdir) and time.time() < deadline:
            time.sleep(0.05)
        assert not _alive(in_old_workdir)
        assert _alive(in_tree), "the reaper of attempt 1 stopped attempt 2's command in the tree"
    finally:
        _finish(gateway, [in_old_workdir, in_tree])
