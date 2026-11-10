from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from superagent.domain import Assignment, Ticket
from superagent.gate import Rules, parse_rules
from superagent.router import Router
from superagent.store import Store

RULES = """
[sources.chat]
policy = "explicit_only"
allow = ["alice"]

[sources.github]
policy = "candidate"
allow = ["alice"]
bots = ["ci-bot"]

[sources.muted]
policy = "ignore"

[[semantic]]
name = "ci-watch"
text = "Investigate a failing main branch."
mode = "act"

[[semantic]]
name = "silent-fix"
text = "Reproduce reported bugs quietly."
"""


@dataclass
class FakeBackend:
    alive: bool = True
    started: list[tuple[str, str, str]] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    on_stop: Callable[[str], None] | None = None
    fail_next_start: bool = False
    output: str = ""
    on_alive_check: Callable[[Assignment], None] | None = None

    def start(self, ticket: Ticket, assignment: Assignment, token: str, context: str = "") -> int:
        if self.fail_next_start:
            self.fail_next_start = False
            raise OSError("cannot start worker")
        self.started.append((ticket.id, assignment.id, token))
        self.contexts.append(context)
        return 4242

    def is_alive(self, assignment: Assignment) -> bool:
        if self.on_alive_check is not None:
            self.on_alive_check(assignment)
        return self.alive

    def last_output(self, assignment: Assignment) -> str:
        return self.output

    def stop(self, assignment: Assignment) -> None:
        self.stopped.append(assignment.id)
        if self.on_stop is not None:
            self.on_stop(assignment.id)


def unlabelled(text: str) -> str:
    """A relayed message as "<State>: <body>", from "<icon> <State> · <title> · <ticket>" and its body lines."""
    head, _, body = text.partition("\n")
    if match := re.fullmatch(r"\S+ (.+?) · .+ · tk-\w+", head):
        return f"{match[1]}: {body}"
    return re.sub(r" \[[^\]]*\]:", ":", text, count=1)


@pytest.fixture
def rules() -> Rules:
    return parse_rules(RULES)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "superagent.db")


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def router(store: Store, rules: Rules, backend: FakeBackend) -> Router:
    return Router(store, rules, backend)
