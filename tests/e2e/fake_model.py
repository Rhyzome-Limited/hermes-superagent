from __future__ import annotations

import argparse
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

GOAL = re.compile(r"scenario=(\w+) id=([\w-]+)")
TRIGGER = re.compile(r"trigger_id=(\S+)")
TICKET = re.compile(r"ticket=(tk-\w+)")
RENDEZVOUS_SECONDS = 120.0
WAIT_SECONDS = 300.0


@dataclass(frozen=True)
class Step:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    rendezvous: bool = False
    wait_for: str = ""


SCRIPTS = {
    "report": [
        Step("superagent_report", {"state": "progress", "summary": "started {id}"}, rendezvous=True),
        Step("superagent_report", {"state": "ready", "summary": "done {id}"}),
    ],
    "guard": [
        Step("terminal", {"command": "touch {work}/guard-{id}"}),
        Step("superagent_report", {"state": "ready", "summary": "done {id}"}),
    ],
    "fence": [
        Step("superagent_report", {"state": "progress", "summary": "started {id}"}),
        Step("superagent_report", {"state": "progress", "summary": "late {id}"}, wait_for="release-{id}"),
        Step("terminal", {"command": "touch {work}/late-{id}"}),
    ],
    "wander": [
        Step("terminal", {"command": "cd /tmp && pwd"}),
        Step("superagent_report", {"state": "ready", "summary": "done {id}"}),
    ],
    "unset": [
        Step("terminal", {"command": "touch {work}/unset-{id}"}),
        Step("superagent_report", {"state": "ready", "summary": "done {id}"}),
    ],
    "route": [
        Step("terminal", {"command": "touch {work}/router-ran-{id}"}),
        Step(
            "superagent_dispatch",
            {"trigger_id": "{trigger}", "goal": "scenario=routed id=routed-worker", "loop": "build-loop"},
        ),
    ],
    "routed": [
        Step("superagent_report", {"state": "progress", "summary": "started {id}"}),
        Step("superagent_report", {"state": "ready", "summary": "done {id}"}),
    ],
    "quiet": [],
    "linger": [
        Step("superagent_report", {"state": "progress", "summary": "started {id}"}),
        Step("superagent_report", {"state": "progress", "summary": "late {id}"}, wait_for="release-{id}"),
    ],
    "cancel": [
        Step("superagent_cancel", {"trigger_id": "{trigger}", "ticket_id": "{ticket}"}),
    ],
    "outsider": [
        Step("superagent_report", {"state": "ready", "summary": "forged {id}"}),
    ],
    "inert": [
        Step("terminal", {"command": "touch {work}/inert-{id}"}),
    ],
    "stop": [
        Step("superagent_report", {"state": "progress", "summary": "started {id}"}),
        Step("terminal", {"command": 'python3 -c "import time; time.sleep(300)" superagent-e2e-{id}'}),
        Step("superagent_report", {"state": "ready", "summary": "should never run {id}"}),
    ],
}

arrivals: set[str] = set()
arrivals_changed = threading.Condition()
log_lock = threading.Lock()


def render(template: str, goal_id: str, work: Path, trigger: str = "", ticket: str = "") -> str:
    rendered = template.replace("{id}", goal_id).replace("{work}", str(work))
    return rendered.replace("{trigger}", trigger).replace("{ticket}", ticket)


def wait_for_partner(goal_id: str) -> bool:
    deadline = time.monotonic() + RENDEZVOUS_SECONDS
    with arrivals_changed:
        arrivals.add(goal_id)
        arrivals_changed.notify_all()
        while len(arrivals) < 2:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            arrivals_changed.wait(remaining)
    return True


def wait_for_file(path: Path) -> bool:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.2)
    return False


def text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def structured_reply(text: str) -> str | None:
    if "You decide which environment events match a team rule" in text:
        rule = re.search(r'"name": "([^"]+)"', text)
        decisions = [
            {"trigger_id": trigger_id, "rule": rule.group(1) if rule else "", "goal": "Investigate the failing CI run"}
            for trigger_id in dict.fromkeys(TRIGGER.findall(text))
        ]
        return json.dumps({"decisions": decisions})
    if "You watch one worker for goal drift" in text:
        return json.dumps({"verdict": "on_track", "reason": "the reports serve the goal"})
    return None


def choose(body: dict[str, Any], work: Path) -> tuple[Step | None, dict[str, Any]]:
    messages = body.get("messages") or []
    tools = sorted(tool.get("function", {}).get("name", "") for tool in body.get("tools") or [])
    match = next(
        (m for m in (GOAL.search(text_of(message)) for message in messages if message["role"] == "user") if m), None
    )
    tool_results = [text_of(message) for message in messages if message.get("role") == "tool"]
    entry: dict[str, Any] = {"at": time.time(), "tools": tools, "tool_results": tool_results}
    if match is None or not tools:
        entry["kind"] = "auxiliary"
        reply = structured_reply(" ".join(text_of(message) for message in messages))
        if reply is not None:
            entry.update(kind="structured", reply=reply)
        return None, entry
    scenario, goal_id = match.groups()
    entry.update(kind="turn", scenario=scenario, id=goal_id, step=len(tool_results))
    script = SCRIPTS[scenario]
    if len(tool_results) >= len(script):
        return None, entry
    step = script[len(tool_results)]
    if step.tool not in tools and "tool_call" not in tools:
        entry["missing_tool"] = step.tool
        return None, entry
    if step.rendezvous:
        entry["overlapped"] = wait_for_partner(goal_id)
    if step.wait_for:
        entry["released"] = wait_for_file(work / render(step.wait_for, goal_id, work))
    entry["sent"] = step.tool
    entry["bridged"] = step.tool not in tools
    found = next((m for m in (TRIGGER.search(text_of(message)) for message in messages) if m), None)
    trigger = found.group(1) if found else ""
    entry["trigger"] = trigger
    named = next((m for m in (TICKET.search(text_of(message)) for message in messages) if m), None)
    ticket = named.group(1) if named else ""
    args = {key: render(value, goal_id, work, trigger, ticket) for key, value in step.args.items()}
    if step.tool in tools:
        return Step(step.tool, args), entry
    return Step("tool_call", {"calls": [{"name": step.tool, "arguments": args}]}), entry


class Handler(BaseHTTPRequestHandler):
    server: FakeModelServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send_json(
            {
                "object": "list",
                "data": [{"id": "fake", "object": "model", "context_length": 200000, "max_completion_tokens": 8000}],
            }
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        step, entry = choose(body, self.server.work)
        with log_lock, self.server.log_path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(entry) + "\n")
        completion = f"chatcmpl-{uuid.uuid4().hex[:8]}"
        tool_call = None
        if step is not None:
            tool_call = {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {"name": step.tool, "arguments": json.dumps(step.args)},
            }
        reply = str(entry.get("reply") or "finished")
        if body.get("stream"):
            self._stream(completion, tool_call, reply)
        else:
            self._complete(completion, tool_call, reply)

    def _complete(self, completion: str, tool_call: dict[str, Any] | None, reply: str) -> None:
        message: dict[str, Any] = {"role": "assistant", "content": None if tool_call else reply}
        if tool_call:
            message["tool_calls"] = [tool_call]
        self._send_json(
            {
                "id": completion,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "fake",
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_call else "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )

    def _stream(self, completion: str, tool_call: dict[str, Any] | None, reply: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def chunk(delta: dict[str, Any], finish: str | None = None) -> None:
            choice = {"index": 0, "delta": delta, "finish_reason": finish}
            payload = {
                "id": completion,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "fake",
                "choices": [choice],
            }
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())

        if tool_call:
            chunk({"role": "assistant", "tool_calls": [{"index": 0, **tool_call}]})
            chunk({}, "tool_calls")
        else:
            chunk({"role": "assistant", "content": reply})
            chunk({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class FakeModelServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, log_path: Path, work: Path) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.log_path = log_path
        self.work = work


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    FakeModelServer(args.port, args.log, args.work).serve_forever()


if __name__ == "__main__":
    main()
