from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from superagent.domain import DEFAULT_LOOP, Loop, Trigger
from superagent.router import Outcome, Router, route_loop
from superagent.store import Store

from .conftest import FakeBackend
from .test_plugin import FakeContext as PluginContext
from .test_plugin import inbound, load_plugin_module, offered

KASTLE = Loop.KASTLE_BUILD


def admit(router: Router, trigger_id: str, text: str = "fix the onboarding screen") -> None:
    trigger = Trigger(trigger_id, "chat", "", "alice", text, "chat:dm", True)
    assert router.ingest(trigger, auto_dispatch=False).outcome is Outcome.ADMITTED
    router.store.take_admitted("chat", "alice", text, 600)


@pytest.mark.parametrize("asked", list(Loop))
@pytest.mark.parametrize(
    ("goal", "repo"),
    [
        ("Fix the krc721 loading spinner in kastle-mobile", ""),
        ("Fix the krc721 loading spinner in KASTLE-MOBILE", ""),
        ("Move the send button", "Rhyzome-Limited/kastle-mobile"),
        ("Move the send button", "kastle"),
        ("Patch Rhyzome-Limited/kastle so the bridge fee shows", ""),
        ("Update the Kastle wallet's receive screen", ""),
        ("Review https://github.com/Rhyzome-Limited/kastle-mobile/pull/12", ""),
        ("Ship CMPY-482 before Friday", ""),
        ("ship cmpy-7", ""),
        ("Fix it", "git@github.com:Rhyzome-Limited/kastle.git"),
    ],
)
def test_kastle_work_always_runs_kastle_build_loop(asked: Loop, goal: str, repo: str) -> None:
    assert route_loop(asked.value, goal, repo) is KASTLE


@pytest.mark.parametrize(
    ("goal", "repo"),
    [
        ("Fix the castle login", ""),
        ("Add a retry to kastle-push", ""),
        ("Bump the kstl-scanner indexer", "Rhyzome-Limited/kstl-scanner"),
        ("Rename kastles to castles in the docs", ""),
        ("Refactor the router", "Rhyzome-Limited/hermes-superagent"),
        ("Count XCMPY-12 rows", ""),
    ],
)
@pytest.mark.parametrize("asked", list(Loop))
def test_other_work_keeps_the_routers_pick(goal: str, repo: str, asked: Loop) -> None:
    assert route_loop(asked.value, goal, repo) is asked


@pytest.mark.parametrize("asked", ["", "claude-multi-loop", "Build-Loop", "kastle-build-loop "])
def test_a_loop_outside_the_fixed_list_is_refused(asked: str) -> None:
    with pytest.raises(ValueError, match="loop must be one of"):
        route_loop(asked, "anything")


def test_a_kastle_ui_fix_asked_as_claude_assisted_loop_is_stored_as_kastle_build_loop(
    router: Router, backend: FakeBackend
) -> None:
    admit(router, "t1", "the onboarding button is cut off in kastle-mobile")
    ticket = router.dispatch("t1", "Fix the cut-off onboarding button in kastle-mobile", "claude-assisted-loop")
    assert ticket.loop == "kastle-build-loop"
    assert router.store.get_ticket(ticket.id).loop == "kastle-build-loop"
    assert router.status()[0]["loop"] == "kastle-build-loop"
    assert len(backend.started) == 1


def test_dispatch_stores_the_routers_loop_and_repo(router: Router) -> None:
    admit(router, "t1")
    ticket = router.dispatch("t1", "Add a CLI flag", "build-loop", "  Rhyzome-Limited/hermes-superagent ")
    stored = router.store.get_ticket(ticket.id)
    assert (stored.loop, stored.repo) == ("build-loop", "Rhyzome-Limited/hermes-superagent")


def test_a_bad_loop_leaves_the_trigger_usable(router: Router, backend: FakeBackend) -> None:
    admit(router, "t1")
    with pytest.raises(ValueError):
        router.dispatch("t1", "Add a CLI flag", "multi-loop")
    assert backend.started == []
    assert router.dispatch("t1", "Add a CLI flag", "build-loop").loop == "build-loop"


def test_work_that_reaches_a_ticket_without_the_router_gets_the_default_loop(router: Router) -> None:
    plain = router.ingest(Trigger("t1", "chat", "", "alice", "tidy the docs", "chat:dm", True))
    kastle = router.ingest(Trigger("t2", "chat", "", "alice", "tidy the kastle-mobile docs", "chat:dm", True))
    assert router.store.get_ticket(plain.ticket_ids[0]).loop == DEFAULT_LOOP.value
    assert router.store.get_ticket(kastle.ticket_ids[0]).loop == KASTLE.value


def test_a_database_from_before_loops_is_upgraded(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE tickets (id TEXT PRIMARY KEY, goal TEXT NOT NULL, mandate TEXT NOT NULL, "
            "channel TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, trigger_id TEXT NOT NULL UNIQUE, "
            "resumed_at REAL)"
        )
        mandate = {
            "policy_version": "v",
            "scope": "g",
            "strength": "explicit",
            "actor": "a",
            "source": "chat",
            "trigger_id": "t0",
        }
        connection.execute(
            "INSERT INTO tickets VALUES ('tk-old', 'g', ?, 'chat:1', 'open', 0, 't0', NULL)", (json.dumps(mandate),)
        )
        connection.execute("PRAGMA user_version = 3")
    store = Store(path)
    old = store.get_ticket("tk-old")
    assert (old.loop, old.repo) == ("", "")
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(tickets)")}
    assert {"loop", "repo"} <= columns
    assert Store(path).get_ticket("tk-old").goal == "g"


SAFE_CONFIG = f"""
[sources.discord]
policy = "explicit_only"
allow = ["u-alice"]

[worker]
argv = [{json.dumps(sys.executable)}, "-c", "pass", "{{brief}}"]
"""


def router_context(home: Path) -> PluginContext:
    (home / "superagent.toml").write_text(SAFE_CONFIG, encoding="utf-8")
    ctx = PluginContext({"role": "router", "home": str(home)})
    load_plugin_module().register(ctx)
    return ctx


def test_dispatch_tool_requires_a_loop_from_the_fixed_list(tmp_path: Path) -> None:
    ctx = router_context(tmp_path)
    schema = ctx.tools["superagent_dispatch"]["schema"]["parameters"]
    assert "loop" in schema["required"]
    assert schema["properties"]["loop"]["enum"] == ["kastle-build-loop", "claude-assisted-loop", "build-loop"]
    assert inbound(ctx, text="fix the send screen in kastle-mobile") is None
    offered(ctx, "fix the send screen in kastle-mobile")
    dispatch = ctx.tools["superagent_dispatch"]["handler"]
    missing = json.loads(dispatch({"trigger_id": "discord:c1:1", "goal": "Fix the kastle-mobile send screen"}))
    assert "loop must be one of" in missing["error"]
    answer = json.loads(
        dispatch(
            {"trigger_id": "discord:c1:1", "goal": "Fix the kastle-mobile send screen", "loop": "claude-assisted-loop"}
        )
    )
    assert answer["loop"] == "kastle-build-loop"
    assert Store(tmp_path / "superagent.db").get_ticket(answer["ticket"]).loop == "kastle-build-loop"
