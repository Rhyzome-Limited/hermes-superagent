from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from superagent.domain import RepairAction, Trigger  # noqa: E402
from superagent.runtime import Runtime, open_runtime  # noqa: E402
from superagent.watchdog import WatchdogPolicy, check_once  # noqa: E402

PORT = 18999
WEBHOOK_PORT = 18644
WORKER_ARGV = ["hermes", "-p", "worker", "chat", "-Q", "--query-file", "{brief}", "-t", "terminal,file,superagent"]
TURN_TIMEOUT = 90.0
failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""), flush=True)
    if not ok:
        failures.append(name)


def wait_until(condition: Callable[[], bool], timeout: float = TURN_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.5)
    return condition()


def hermes(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["hermes", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)


def write_home(home: Path, *, allow_same_user: bool) -> None:
    home.mkdir(parents=True, exist_ok=True)
    argv = json.dumps(WORKER_ARGV)
    (home / "superagent.toml").write_text(
        '[sources.chat]\npolicy = "explicit_only"\nallow = ["alice"]\n\n'
        f"[worker]\nargv = {argv}\nallow_same_user = {str(allow_same_user).lower()}\n",
        encoding="utf-8",
    )


def create_profile(hermes_home: Path, name: str) -> Path:
    created = hermes("profile", "create", name)
    print(f"profile create {name}:", created.returncode, created.stderr[-300:], flush=True)
    profile = hermes_home / "profiles" / name
    target = profile / "plugins" / "hermes-superagent"
    shutil.copytree(REPO, target, ignore=shutil.ignore_patterns(".git", "__pycache__", "assets", ".venv"))
    enabled = hermes("-p", name, "plugins", "enable", "hermes-superagent")
    print(f"plugins enable {name}:", enabled.returncode, enabled.stderr[-300:], flush=True)
    return profile


def set_up_worker_profile(hermes_home: Path, home: Path) -> None:
    configure_worker(create_profile(hermes_home, "worker"), home)


def configure_worker(
    profile: Path,
    home: Path,
    *,
    tool_search: str = "auto",
    role: str | None = "worker",
    extra: dict[str, Any] | None = None,
) -> None:
    config_path = profile / "config.yaml"
    config: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    config = config or {}
    config["model"] = {
        "provider": "custom",
        "base_url": f"http://127.0.0.1:{PORT}/v1",
        "default": "fake",
        "api_key": "not-a-real-key",
    }
    config.setdefault("tools", {})["tool_search"] = {"enabled": tool_search}
    if role is not None:
        plugins = config.setdefault("plugins", {})
        plugins.setdefault("entries", {})["hermes-superagent"] = {"settings": {"role": role, "home": str(home)}}
    config.update(extra or {})
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")


def start_task(runtime: Runtime, goal: str) -> str:
    trigger = Trigger(
        id=f"e2e-{uuid.uuid4().hex[:8]}",
        source="chat",
        thread="",
        actor="alice",
        text=goal,
        channel="chat:e2e",
        addressed=True,
    )
    return runtime.router.ingest(trigger).ticket_ids[0]


def model_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def turns(path: Path, goal_id: str) -> list[dict[str, Any]]:
    return [entry for entry in model_log(path) if entry.get("id") == goal_id]


def report_states(runtime: Runtime, ticket: str) -> list[str]:
    return [report.state.value for report in runtime.store.ticket_reports(ticket)]


def scenario_overlap(runtime: Runtime, log: Path) -> None:
    tickets = {goal_id: start_task(runtime, f"scenario=report id={goal_id}") for goal_id in ("alpha", "beta")}
    for goal_id, ticket in tickets.items():
        wait_until(lambda t=ticket: "ready" in report_states(runtime, t))
        check(
            f"{goal_id}: can call superagent_report",
            any(e.get("sent") == "superagent_report" for e in turns(log, goal_id)),
        )
        check(f"{goal_id}: finishes with a stored ready report", "ready" in report_states(runtime, ticket))
        summaries = [report.summary for report in runtime.store.ticket_reports(ticket)]
        check(f"{goal_id}: reports land on its own ticket", all(goal_id in s for s in summaries), summaries)
    check("deferred plugin tools work through the bridge", any(e.get("bridged") for e in turns(log, "alpha")))
    check(
        "two workers run at the same time",
        all(any(e.get("overlapped") for e in turns(log, goal_id)) for goal_id in tickets),
    )


def scenario_guard(runtime: Runtime, log: Path, work: Path) -> None:
    ticket = start_task(runtime, "scenario=guard id=guarded")
    wait_until(lambda: "ready" in report_states(runtime, ticket))
    results = [result for entry in turns(log, "guarded") for result in entry["tool_results"]]
    check("guard: terminal is refused on a local backend", any("same OS user" in r for r in results), results[:2])
    check("guard: the blocked command never ran", not (work / "guard-guarded").exists())
    check("guard: the worker can still report", "ready" in report_states(runtime, ticket))


def scenario_quiet(runtime: Runtime, log: Path) -> None:
    ticket = start_task(runtime, "scenario=quiet id=quiet")
    assignment = next(a for a in runtime.store.running_assignments() if a.ticket_id == ticket)
    exited = wait_until(lambda: not runtime.router.backend.is_alive(assignment), timeout=120.0)
    check("quiet: the worker ends its turn without reporting", exited)
    patient = WatchdogPolicy(stall_after_seconds=3600, orphan_after_seconds=3600)
    parked = [result for result in check_once(runtime.router, policy=patient) if result.ticket_id == ticket]
    check("quiet: the watchdog parks it for a person", [r.action for r in parked] == [RepairAction.PARK], parked)
    questions = [report.question for report in runtime.store.ticket_reports(ticket)]
    check("quiet: the question carries the worker's last words", any("finished" in q for q in questions), questions)
    check("quiet: the task is not run a second time", len(turns(log, "quiet")) == 1, turns(log, "quiet"))


def scenario_fence(runtime: Runtime, log: Path, work: Path) -> None:
    ticket = start_task(runtime, "scenario=fence id=fenced")
    wait_until(lambda: "progress" in report_states(runtime, ticket))
    assignment = next(a for a in runtime.store.running_assignments() if a.ticket_id == ticket)
    before = len(report_states(runtime, ticket))
    runtime.store.end_assignment(assignment.id)
    (work / "release-fenced").touch()
    wait_until(lambda: len(turns(log, "fenced")) >= 4, timeout=120.0)
    check("fence: a late report is not stored", len(report_states(runtime, ticket)) == before)
    check("fence: a late tool call never runs", not (work / "late-fenced").exists())


def scenario_outsider(runtime: Runtime, log: Path, work: Path) -> None:
    brief = work / "outsider.md"
    brief.write_text("scenario=outsider id=forged", encoding="utf-8")
    subprocess.run(
        ["hermes", "-p", "worker", "chat", "-Q", "--query-file", str(brief), "-t", "terminal,file,superagent"],
        cwd=work,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=TURN_TIMEOUT,
        check=False,
    )
    results = [result for entry in turns(log, "forged") for result in entry["tool_results"]]
    check("outsider: a session outside a worker directory is refused", any("workers" in r for r in results), results)
    stored = [
        report.summary for ticket in runtime.store.live_tickets() for report in runtime.store.ticket_reports(ticket.id)
    ]
    check("outsider: the forged report is not stored", not any("forged" in summary for summary in stored), stored)


def scenario_wander(runtime: Runtime) -> None:
    ticket = start_task(runtime, "scenario=wander id=wandered")
    wait_until(lambda: "ready" in report_states(runtime, ticket))
    check("wander: changing directory in a terminal doesn't change identity", "ready" in report_states(runtime, ticket))


def scenario_role_unset(runtime: Runtime, log: Path, work: Path, profile: Path, home: Path) -> None:
    configure_worker(profile, home, tool_search="off", role="")
    ticket = start_task(runtime, "scenario=unset id=unset")
    assignment = next(a for a in runtime.store.running_assignments() if a.ticket_id == ticket)
    exited = wait_until(lambda: not runtime.router.backend.is_alive(assignment), timeout=120.0)
    entries = turns(log, "unset")
    results = [result for entry in entries for result in entry["tool_results"]]
    offered = any("superagent_report" in entry["tools"] for entry in entries)
    refused = any("only works in the worker" in r for r in results)
    check("no role set: the session ends", exited)
    check("no role set: the plugin's hooks stay inert there", not any("same OS user" in r for r in results), results)
    check("no role set: a worker directory gives no role, so a report is refused", refused or not offered, results)
    check("no role set: nothing is reported", "ready" not in report_states(runtime, ticket))
    runtime.store.end_assignment(assignment.id)
    runtime.store.end_ticket(ticket)


STOP_MARKER = "superagent-e2e-stopped"  # on the worker's own command line, so another process can't stand in for it


def scenario_stop(runtime: Runtime, log: Path, work: Path) -> None:
    (work / STOP_MARKER).symlink_to(shutil.which("sleep") or "/bin/sleep")
    ticket = start_task(runtime, "scenario=stop id=stopped")
    running = wait_until(lambda: subprocess.run(["pgrep", "-f", STOP_MARKER], check=False).returncode == 0)
    check("stop: the worker's command started", running, turns(log, "stopped")[-3:])
    assignment = next((a for a in runtime.store.running_assignments() if a.ticket_id == ticket), None)
    if assignment is None:
        check("stop: the assignment is still running", False)
        return
    runtime.store.end_assignment(assignment.id)
    runtime.router.backend.stop(assignment)
    gone = wait_until(lambda: subprocess.run(["pgrep", "-f", STOP_MARKER], check=False).returncode != 0, timeout=60.0)
    check("stop: the worker's command is gone", gone)
    settled = len(turns(log, "stopped"))
    time.sleep(10)
    check("stop: no more model calls", len(turns(log, "stopped")) == settled)
    check("stop: the client process is gone", not runtime.router.backend.is_alive(assignment))


def post_webhook(route: str, message: str, profile: str = "router") -> int:
    return post_json(route, {"message": message}, {"X-Request-ID": uuid.uuid4().hex}, profile)


def post_github_comment(login: str, body: str) -> int:
    payload = {
        "action": "created",
        "issue": {"number": 7, "title": "Flaky checkout test", "user": {"login": "alice", "type": "User"}},
        "comment": {"body": body, "user": {"login": login, "type": "User"}},
        "repository": {"full_name": "acme/app"},
        "sender": {"login": login, "type": "User"},
    }
    headers = {"X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": uuid.uuid4().hex}
    return post_json("github", payload, headers)


def post_json(route: str, payload: dict[str, Any], headers: dict[str, str], profile: str = "router") -> int:
    request = urllib.request.Request(
        f"http://127.0.0.1:{WEBHOOK_PORT}/p/{profile}/webhooks/{route}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return int(response.status)


def webhook_is_up() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{WEBHOOK_PORT}/health", timeout=3) as response:
            return bool(response.status == 200)
    except OSError:
        return False


def stored_summaries(home: Path) -> list[str]:
    database = home / "superagent.db"
    if not database.exists():
        return []
    with sqlite3.connect(database) as connection:
        return [row[0] for row in connection.execute("SELECT summary FROM reports")]


def stage_router(work: Path, log: Path) -> None:
    hermes_home = work / "router-home"
    hermes_home.mkdir()
    os.environ["HERMES_HOME"] = str(hermes_home)
    home = work / "shared"
    sent = work / "sent.tsv"
    tasks = work / "tasks.json"
    tasks.write_text(json.dumps([{"id": "t1", "content": "Renew the domain"}, {"id": "t2", "content": "Pay rent"}]))
    tasks_argv = json.dumps(["python3", "-c", "import sys; print(open(sys.argv[1]).read())", str(tasks)])
    write_home(home, allow_same_user=False)
    with (home / "superagent.toml").open("a", encoding="utf-8") as config:
        argv = json.dumps(["python3", str(Path(__file__).with_name("outbound.py")), str(sent), "{channel}", "{text}"])
        config.write(
            f'\n[sources."webhook:inbox"]\npolicy = "explicit_only"\nallow = ["webhook:inbox"]\nvia_router = true\n'
            f'\n[sources."webhook:github"]\npolicy = "explicit_only"\nallow = ["alice"]\nnotify = "discord:team"\n'
            f'\n[sources."webhook:ci"]\npolicy = "candidate"\nbots = ["github-actions[bot]"]\n'
            f'\n[digest]\nchannel = "discord:team"\n'
            f'\n[channels]\nalerts = "discord:alerts"\nbrief = "discord:brief"\n'
            f"\n[brief]\nhour = 0\n"
            f"\n[tick]\nalert_after_seconds = 0\n"
            f'\n[connectors.tasks]\ntype = "command"\nevery = "0s"\nargv = {tasks_argv}\n'
            'fields = { id = "id", title = "content" }\n'
            '\n[connectors.agents-here]\ntype = "agents"\nevery = "0s"\n'
            'channel = "discord:agents"\nnotify_detail = true\n'
            f"\n[outbound]\nargv = {argv}\n"
            f'\n[[semantic]]\nname = "ci-watch"\ntext = "Look into failing CI runs on main."\nmode = "record"\n'
        )
    route = {"secret": "INSECURE_NO_AUTH", "prompt": "{message}", "profile": "router"}
    github = {"secret": "INSECURE_NO_AUTH", "prompt": "{comment.body}", "profile": "router"}
    ci = {"secret": "INSECURE_NO_AUTH", "prompt": "CI {workflow_run.name} ended with {workflow_run.conclusion}"}
    routes = {
        "inbox": route,
        "stranger": route,
        "github": github,
        "ci": {**ci, "profile": "router"},
        "plain": {**route, "profile": "owner"},
    }
    listener = {
        "gateway": {"multiplex_profiles": True},
        "platforms": {
            "webhook": {"enabled": True, "extra": {"host": "127.0.0.1", "port": WEBHOOK_PORT, "routes": routes}}
        },
    }
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(listener), encoding="utf-8")
    configure_worker(create_profile(hermes_home, "router"), home, role="router")
    configure_worker(create_profile(hermes_home, "worker"), home)
    configure_worker(create_profile(hermes_home, "watchdog"), home, role="watchdog")
    owner_tools = {"platform_toolsets": {"webhook": ["terminal"]}}
    configure_worker(create_profile(hermes_home, "owner"), home, role=None, tool_search="off", extra=owner_tools)
    interpreter = Path(shutil.which("hermes") or "hermes").resolve().parent / "python"
    extra = subprocess.run(
        [str(interpreter), "-c", "import pm; pm.sync_venv(['sms'], explicit=True)"],
        capture_output=True,
        text=True,
        check=False,
    )
    print("webhook dependency:", extra.returncode, extra.stderr[-500:], flush=True)
    with (work / "host.log").open("ab") as host_log:
        host = subprocess.Popen(
            ["hermes", "gateway", "run"], stdin=subprocess.DEVNULL, stdout=host_log, stderr=subprocess.STDOUT
        )
    try:
        check("router: the host gateway serves the webhook", wait_until(webhook_is_up, timeout=120.0))
        scenario_inert(log, work)
        post_webhook("stranger", "scenario=route id=stranger")
        time.sleep(15)
        check(
            "router: a stranger's message never reaches a model", not turns(log, "stranger"), turns(log, "stranger")[:1]
        )
        post_webhook("inbox", "scenario=route id=asked")
        finished = wait_until(lambda: "done routed-worker" in stored_summaries(home), timeout=180.0)
        entries = turns(log, "asked")
        check("router: the model is offered the admitted trigger", any(e.get("trigger") for e in entries), entries[:1])
        results = [result for entry in entries for result in entry["tool_results"]]
        check("router: terminal is refused", any("never executes" in r for r in results), results[:2])
        check("router: the refused command never ran", not (work / "router-ran-asked").exists())
        check("router: dispatch opens a ticket", any('"ticket"' in r for r in results), results)
        check("router: the dispatched worker reports ready", finished, stored_summaries(home))
        post_github_comment("mallory", "scenario=routed id=gh-stranger")
        post_github_comment("alice", "scenario=routed id=gh-worker")
        github_done = wait_until(lambda: "done gh-worker" in stored_summaries(home), timeout=180.0)
        check("github: an allowed person's comment starts a worker", github_done, stored_summaries(home))
        check(
            "github: a stranger's comment starts nothing", not turns(log, "gh-stranger"), turns(log, "gh-stranger")[:1]
        )
        check(
            "github: the router's model never reads the event",
            not any("clarify" in entry["tools"] for entry in turns(log, "gh-worker")),
        )
        os.environ["SUPERAGENT_SESSIONS_DIR"] = str(work / "sessions")
        permission = {
            "session_id": "e2e-session",
            "hook_event_name": "PermissionRequest",
            "cwd": str(work / "site"),
            "tool_name": "Bash",
            "tool_input": {"command": "make deploy"},
        }
        hooked = subprocess.run(
            ["python3", str(REPO / "superagent" / "agents" / "hook.py")],
            input=json.dumps(permission),
            capture_output=True,
            text=True,
            check=False,
        )
        print("hook:", hooked.returncode, hooked.stdout, hooked.stderr[-300:], flush=True)
        tick = hermes("-p", "watchdog", "superagent", "tick")
        print("tick:", tick.returncode, tick.stdout[-600:], tick.stderr[-600:], flush=True)
        check("watchdog: the tick runs from the CLI", tick.returncode == 0)
        listed = hermes("-p", "watchdog", "superagent", "ledger", "list", "--json", "--connector", "tasks")
        titles = sorted(item["title"] for item in json.loads(listed.stdout or "[]")) if listed.returncode == 0 else []
        second = hermes("-p", "watchdog", "superagent", "tick")
        print("second tick:", second.returncode, second.stdout[-300:], flush=True)
        sent_now = sent.read_text(encoding="utf-8") if sent.exists() else ""
        check(
            "agents: a session still waiting on a permission reaches the agents channel",
            "discord:agents\tsite [agents-here] is waiting: permission for Bash (it-" in sent_now,
            sent_now[-400:],
        )
        briefed = any(
            line.startswith("discord:brief\t") and ("Brief for" in line or "(1/" in line)
            for line in sent_now.splitlines()
        )
        check("brief: the first tick of the day sends the brief", briefed, sent_now[-300:])
        board = hermes("-p", "watchdog", "superagent", "agents")
        check("agents: the board lists it", "waiting" in board.stdout and "site" in board.stdout, board.stdout[-300:])
        check(
            "ledger: the tick polled a command connector into the ledger",
            titles == ["Pay rent", "Renew the domain"],
            listed.stdout[-300:] + listed.stderr[-300:],
        )
        delivered = sent.read_text(encoding="utf-8") if sent.exists() else ""
        check("watchdog: the result is relayed to the person", "]: done routed-worker" in delivered, delivered)
        back_to_chat = any(
            line.startswith("webhook:") and "done routed-worker" in line for line in delivered.splitlines()
        )
        check("watchdog: the relay goes back to the webhook chat", back_to_chat, delivered)
        check("github: the result goes to the team channel", "discord:team\t✅ Done · " in delivered, delivered)
        check("relay: messages name their task", "· tk-" in delivered and "]: done gh-worker" in delivered, delivered)
        config_path = home / "superagent.toml"
        good = config_path.read_text(encoding="utf-8")
        config_path.write_text(good.replace('policy = "explicit_only"', 'policy = "explicit_only', 1), encoding="utf-8")
        post_github_comment("alice", "scenario=routed id=gh-after-typo")
        survived = wait_until(lambda: "done gh-after-typo" in stored_summaries(home), timeout=180.0)
        check("config: a broken edit keeps the last good config running", survived, stored_summaries(home))
        broken_tick = hermes("-p", "watchdog", "superagent", "tick")
        loud = broken_tick.returncode != 0 and "last good config" in broken_tick.stdout
        check("config: every tick fails loudly until it's fixed", loud, broken_tick.stdout[-500:])
        alerted = sent.read_text(encoding="utf-8") if sent.exists() else ""
        check("alerts: the broken config reaches the alerts channel", "discord:alerts\tAlert: config:" in alerted)
        config_path.write_text(good, encoding="utf-8")
        scenario_cancel(open_runtime(home), log, work, sent)
        scenario_digest(sent)
    finally:
        host.terminate()
        host.wait(timeout=30)


def scenario_inert(log: Path, work: Path) -> None:
    post_webhook("plain", "scenario=inert id=owner-plain", profile="owner")
    ran = wait_until(lambda: (work / "inert-owner-plain").exists(), timeout=120.0)
    entries = turns(log, "owner-plain")
    offered = sorted({tool for entry in entries for tool in entry["tools"] if tool.startswith("superagent")})
    results = [result for entry in entries for result in entry["tool_results"]]
    check("inert: a message to a profile with the plugin and no role reaches its model", bool(entries))
    check("inert: that profile's terminal runs", ran, results[:2])
    check("inert: no superagent tool is offered there", not offered, offered)


def scenario_environment_untouched(hermes_home: Path) -> None:
    roots = [hermes_home, Path(shutil.which("hermes") or "hermes").resolve().parents[1]]
    leaked = [str(path) for root in roots for path in root.rglob("site-packages/pytest")]
    check("enabling the plugin puts no dev tools in the Hermes environment", not leaked, leaked)


def scenario_cancel(runtime: Runtime, log: Path, work: Path, sent: Path) -> None:
    trigger = Trigger(
        id="linger-1",
        source="webhook:inbox",
        thread="",
        actor="webhook:inbox",
        text="scenario=linger id=lingering",
        channel="webhook:linger",
        addressed=True,
    )
    ticket = runtime.router.ingest(trigger).ticket_ids[0]
    wait_until(lambda: "progress" in report_states(runtime, ticket))
    assignment = next(a for a in runtime.store.running_assignments() if a.ticket_id == ticket)
    post_webhook("inbox", f"scenario=cancel id=cancel-request ticket={ticket}")
    ended = wait_until(lambda: runtime.store.get_ticket(ticket).status.value == "ended", timeout=120.0)
    check("cancel: the router's model stops a ticket for the person who asked", ended, turns(log, "cancel-request"))
    gone = wait_until(lambda: not runtime.router.backend.is_alive(assignment), timeout=60.0)
    check("cancel: the worker's client is gone", gone)
    (work / "release-lingering").touch()
    time.sleep(5)
    summaries = [report.summary for report in runtime.store.ticket_reports(ticket)]
    check("cancel: the worker's late report is never stored", "late lingering" not in summaries, summaries)
    hermes("-p", "watchdog", "superagent", "tick")
    delivered = sent.read_text(encoding="utf-8") if sent.exists() else ""
    check("cancel: the person is told", "⏹️ Cancelled · " in delivered, delivered[-400:])


def scenario_digest(sent: Path) -> None:
    payload = {
        "action": "completed",
        "workflow_run": {"name": "tests", "conclusion": "failure"},
        "repository": {"full_name": "acme/app"},
        "sender": {"login": "github-actions[bot]", "type": "Bot"},
    }
    post_json("ci", payload, {"X-GitHub-Event": "workflow_run", "X-GitHub-Delivery": uuid.uuid4().hex})
    time.sleep(5)
    assessed = hermes("-p", "watchdog", "superagent", "tick")
    print("assess tick:", assessed.returncode, assessed.stdout[-400:], assessed.stderr[-400:], flush=True)
    digest = hermes("-p", "watchdog", "superagent", "digest", "--send")
    print("digest:", digest.returncode, digest.stdout[-1200:], digest.stderr[-400:], flush=True)
    check("digest: a CI bot event is assessed, not acted on", "Investigate the failing CI run" in digest.stdout)
    check("digest: it names the record-mode rule", "ci-watch, 1:" in digest.stdout, digest.stdout[-600:])
    check("digest: it lists finished work", "Done (" in digest.stdout)
    hermes("-p", "watchdog", "superagent", "tick")
    delivered = sent.read_text(encoding="utf-8") if sent.exists() else ""
    check("digest: it reaches the digest channel", "discord:team\tSuperagent, last 24 hours" in delivered)


def scenario_host_env() -> None:
    found = subprocess.run(["pgrep", "-f", "gateway run"], capture_output=True, text=True, check=False)
    pids = found.stdout.split()
    if not Path("/proc").is_dir():
        # Without /proc (macOS), only the gateways this run's HERMES_HOME recorded: never another install's.
        pids = [pid for pid in pids if pid in recorded_gateway_pids(Path(os.environ["HERMES_HOME"]))]
    check("a host gateway is running", bool(pids))
    for pid in pids:
        check(f"host gateway {pid} holds no SUPERAGENT_ variable", "SUPERAGENT_" not in environment_of(pid))


def recorded_gateway_pids(hermes_home: Path) -> set[str]:
    pids: set[str] = set()
    for path in [hermes_home / "gateway.pid", *hermes_home.glob("profiles/*/gateway.pid")]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = record.get("pid") if isinstance(record, dict) else record
        if isinstance(pid, int):
            pids.add(str(pid))
    return pids


def environment_of(pid: str) -> str:
    environ = Path(f"/proc/{pid}/environ")
    if environ.exists():
        return environ.read_bytes().decode(errors="replace")
    argv = ["ps", "-E", "-ww", "-o", "command=", "-p", pid]
    return subprocess.run(argv, capture_output=True, text=True, check=False).stdout


def show_logs(hermes_home: Path, work: Path) -> None:
    paths = sorted(work.glob("**/worker.log")) + sorted(work.glob("host.log"))
    paths += sorted(hermes_home.glob("**/logs/*.log")) + sorted(work.glob("router-home/**/logs/*.log"))
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        keep = 16000 if "router-home/logs/gateway.log" in str(path) else 3000
        print(f"\n--- {path}\n{text[-keep:]}", flush=True)
    print("\n--- model log", flush=True)
    log = work / "model.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines()[-40:]:
            print(line[:700], flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    work: Path = args.work
    work.mkdir(parents=True, exist_ok=True)
    hermes_home = Path(os.environ["HERMES_HOME"])
    guarded_home = work / "guarded"
    open_home = work / "open"
    write_home(guarded_home, allow_same_user=False)
    write_home(open_home, allow_same_user=True)
    set_up_worker_profile(hermes_home, guarded_home)
    log = work / "model.jsonl"
    model = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("fake_model.py")),
            "--port",
            str(PORT),
            "--log",
            str(log),
            "--work",
            str(work),
        ]
    )
    try:
        guarded = open_runtime(guarded_home)
        scenario_overlap(guarded, log)
        configure_worker(hermes_home / "profiles" / "worker", guarded_home, tool_search="off")
        scenario_guard(guarded, log, work)
        scenario_quiet(guarded, log)
        scenario_fence(guarded, log, work)
        scenario_outsider(guarded, log, work)
        scenario_role_unset(guarded, log, work, hermes_home / "profiles" / "worker", guarded_home)
        configure_worker(hermes_home / "profiles" / "worker", open_home, tool_search="off")
        opened = open_runtime(open_home)
        scenario_wander(opened)
        scenario_stop(opened, log, work)
        scenario_host_env()
        scenario_environment_untouched(hermes_home)
        stage_router(work, log)
    finally:
        show_logs(hermes_home, work)
        model.terminate()
    print(f"\n{len(failures)} failure(s)" + (": " + "; ".join(failures) if failures else ""), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
