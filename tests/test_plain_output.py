"""Tuning plan Phase A: the report tool's style gate, the Started message, and the code-answered commands."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from superagent import style
from superagent.config import DB_FILENAME, WORKDIRS_DIRNAME
from superagent.domain import Check, ReportFields, ReportState
from superagent.runtime import open_runtime
from superagent.store import Store
from superagent.worker import IDENTITY_FILENAME

from .test_outbound import RECORD
from .test_plugin import FakeContext, inbound, load_plugin_module, offered, use_terminal_backend, worker_session

SOURCES = """
[sources.discord]
policy = "explicit_only"
allow = ["u-alice"]
"""

OUTCOME_34_WORDS = (
    "The worker moved the relay guard job to macOS because the six bash tests need the old shell and Linux "
    "only has the new one so they failed there before and now they pass."
)
GOOD_READY = {
    "state": "ready",
    "outcome": "The relay-guard tests now run on macOS. All 3 CI jobs pass.",
    "changes": ["Changed the CI file only. No test changed."],
    "checks": [{"name": "relay-guard", "passed": True}, {"name": "wiki-mirror", "passed": True}],
    "links": ["https://github.com/Rhyzome-Limited/rhyzome-superagent-guards/pull/4"],
    "next_for_leo": "Review and merge PR #4.",
    "details": "commit 2c512b5, run 42756344802: relay-guard 364 passed",
}


def home_with_outbound(home: Path) -> Path:
    """A home whose [outbound] argv appends each sent message to sent.jsonl."""
    script = home / "record.py"
    script.write_text(RECORD, encoding="utf-8")
    sent = home / "sent.jsonl"
    argv = [sys.executable, str(script), str(sent), "--to", "{channel}", "--", "{text}"]
    worker = [sys.executable, "-c", "pass", "{brief}"]
    config = f"{SOURCES}\n[worker]\nargv = {json.dumps(worker)}\n\n[outbound]\nargv = {json.dumps(argv)}\n"
    (home / "superagent.toml").write_text(config, encoding="utf-8")
    return sent


def sent_texts(sent: Path) -> list[str]:
    return [json.loads(line)[-1] for line in sent.read_text().splitlines()] if sent.exists() else []


def context(role: str, home: Path) -> FakeContext:
    ctx = FakeContext({"role": role, "home": str(home)})
    load_plugin_module().register(ctx)
    return ctx


@pytest.fixture
def worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Store, str]:
    home_with_outbound(tmp_path)
    ctx = context("worker", tmp_path)
    store = Store(tmp_path / DB_FILENAME)
    ctx.cwd, assignment_id = worker_session(tmp_path, store)
    use_terminal_backend(monkeypatch, "docker")
    return ctx.tools["superagent_report"]["handler"], store, assignment_id


def test_a_34_word_outcome_is_refused_with_a_rewrite_error(worker: tuple[Any, Store, str]) -> None:
    report, store, assignment_id = worker
    answer = json.loads(report({"state": "ready", "outcome": OUTCOME_34_WORDS}))
    assert "outcome has 34 words; the limit is 20. Rewrite it." in answer["error"]
    assert store.reports_for(assignment_id) == []
    assert store.get_assignment(assignment_id).status.value == "running"


def test_a_good_report_after_a_refusal_is_accepted(worker: tuple[Any, Store, str]) -> None:
    report, store, assignment_id = worker
    assert "error" in json.loads(report({"state": "ready", "outcome": OUTCOME_34_WORDS}))
    assert json.loads(report(GOOD_READY))["reported"] == "ready"
    [stored] = store.reports_for(assignment_id)
    assert stored.fields is not None and stored.fields.outcome == GOOD_READY["outcome"]
    assert stored.fields.details == GOOD_READY["details"]
    assert not stored.style_fail


def test_after_2_failed_tries_the_code_only_message_is_used_and_style_fail_is_logged(
    worker: tuple[Any, Store, str], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    report, store, assignment_id = worker
    bad = {"state": "ready", "outcome": OUTCOME_34_WORDS, "details": "commit 2c512b5"}
    assert "error" in json.loads(report(bad))
    second = json.loads(report(bad))["error"]
    assert "last try" in second
    with caplog.at_level(logging.WARNING):
        assert json.loads(report(bad))["reported"] == "ready"
    [stored] = store.reports_for(assignment_id)
    assert stored.style_fail
    assert any("style_fail" in record.getMessage() for record in caplog.records)
    [(_, _, text)] = open_runtime(tmp_path).router.relay()
    ticket_id = store.get_assignment(assignment_id).ticket_id
    assert text == f'✅ Done · g · {ticket_id}\nGate passed. Send "details {ticket_id}" for more.'


def test_summary_is_an_alias_of_details(worker: tuple[Any, Store, str]) -> None:
    report, store, assignment_id = worker
    assert json.loads(report({"state": "progress", "summary": "reading ci.yml"}))["reported"] == "progress"
    [stored] = store.reports_for(assignment_id)
    assert stored.fields is not None and stored.fields.details == "reading ci.yml"


def test_an_old_worker_without_an_outcome_is_told_how_to_write_one(worker: tuple[Any, Store, str]) -> None:
    report, _, _ = worker
    error = json.loads(report({"state": "ready", "summary": "PR #4 · cause: bash 5 (6 fails)"}))["error"]
    assert "outcome is missing" in error


def test_progress_reports_never_reach_chat_so_they_are_not_checked(worker: tuple[Any, Store, str]) -> None:
    report, _, _ = worker
    assert json.loads(report({"state": "progress", "outcome": OUTCOME_34_WORDS}))["reported"] == "progress"


def test_blocked_needs_a_question(worker: tuple[Any, Store, str]) -> None:
    report, _, _ = worker
    assert (
        "question is missing"
        in json.loads(report({"state": "blocked", "outcome": "The ticket names no count."}))["error"]
    )


def test_an_approval_park_is_stored_and_names_the_reply(worker: tuple[Any, Store, str], tmp_path: Path) -> None:
    report, store, assignment_id = worker
    park = {
        "state": "blocked",
        "outcome": "The plan is ready.",
        "question": "Approve the plan for the push retry fix?",
        "approve": True,
        "details": "Plan at /Users/leonardo/superagent/p2/plans/x.md: retry 3 times, then stop.",
    }
    assert json.loads(report(park))["reported"] == "blocked"
    [stored] = store.reports_for(assignment_id)
    assert stored.fields is not None and stored.fields.approve
    ticket_id = store.get_assignment(assignment_id).ticket_id
    [(_, _, text)] = open_runtime(tmp_path).router.relay()
    assert text.endswith(f'\nReply "approve {ticket_id}" or say what to change.')


def test_too_many_changes_are_refused(worker: tuple[Any, Store, str]) -> None:
    report, _, _ = worker
    answer = json.loads(report({**GOOD_READY, "changes": ["One.", "Two.", "Three.", "Four."]}))
    assert "changes has 4 items; the limit is 3." in answer["error"]


def router_home(tmp_path: Path) -> tuple[FakeContext, Path]:
    sent = home_with_outbound(tmp_path)
    return context("router", tmp_path), sent


def admitted_trigger(ctx: FakeContext, text: str = "fix the push retry") -> str:
    assert inbound(ctx, text=text) is None
    return str(offered(ctx, text)["context"].split("trigger_id=")[1].split("\n")[0])


def dispatch(ctx: FakeContext, trigger_id: str, session: str = "s-chat", **args: str) -> dict[str, Any]:
    """superagent_dispatch as a chat turn calls it: Hermes passes the turn's session id."""
    payload = {"trigger_id": trigger_id, "goal": "Fix the push retry in kastle-push.", "loop": "claude-assisted-loop"}
    handler = ctx.tools["superagent_dispatch"]["handler"]
    result: dict[str, Any] = json.loads(handler({**payload, **args}, session_id=session))
    return result


def reply_after(ctx: FakeContext, session: str = "s-chat", text: str = "[SILENT]") -> Any:
    """What the router's own reply becomes: transform_llm_output at the end of the turn."""
    return ctx.hooks["transform_llm_output"](
        response_text=text, session_id=session, model="m", platform="discord", turn_id="turn-1"
    )


def test_dispatch_needs_a_short_title(tmp_path: Path) -> None:
    ctx, _ = router_home(tmp_path)
    trigger_id = admitted_trigger(ctx)
    assert "title is required" in dispatch(ctx, trigger_id)["error"]
    assert (
        "title has 7 words; the limit is 5."
        in dispatch(ctx, trigger_id, title="Fix the push retry limit now please")["error"]
    )
    assert "ticket" in dispatch(ctx, trigger_id, title="Push retry limit")


def test_one_dispatch_on_a_chat_turn_is_exactly_one_message_the_started_template(tmp_path: Path) -> None:
    # Hermes 0.22.7 refuses a bare [SILENT] on a DM turn and sends its own notice, so the Started message is the
    # router's reply itself: code writes it, nothing goes through the outbox, and the chat gets one message.
    ctx, sent = router_home(tmp_path)
    trigger_id = admitted_trigger(ctx)
    args = {"title": "Push retry limit", "repo": "Rhyzome-Limited/kastle-push", "done_when": "CI passes"}
    answer = dispatch(ctx, trigger_id, **args)
    assert sent_texts(sent) == []
    started = f"▶️ Started · Push retry limit · {answer['ticket']}\nRepo: kastle-push.\nDone when: CI passes."
    assert reply_after(ctx) == started
    assert style.check_message(started) == []
    assert reply_after(ctx) is None
    assert dispatch(ctx, trigger_id, **args)["ticket"] == answer["ticket"]
    assert reply_after(ctx) is None and sent_texts(sent) == []


def test_the_started_reply_belongs_to_its_own_session(tmp_path: Path) -> None:
    ctx, _ = router_home(tmp_path)
    dispatch(ctx, admitted_trigger(ctx), session="s-a", title="Push retry limit")
    assert reply_after(ctx, session="s-b") is None
    assert reply_after(ctx, session="s-a").startswith("▶️ Started · Push retry limit · tk-")


def test_a_stale_started_reply_is_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _ = router_home(tmp_path)
    dispatch(ctx, admitted_trigger(ctx), title="Push retry limit")
    later = time.time() + 3600
    monkeypatch.setattr(time, "time", lambda: later)
    assert reply_after(ctx) is None


def test_a_dispatch_without_a_session_sends_started_through_the_outbox(tmp_path: Path) -> None:
    ctx, sent = router_home(tmp_path)
    answer = dispatch(ctx, admitted_trigger(ctx), session="", title="Push retry limit")
    assert sent_texts(sent) == [
        f"▶️ Started · Push retry limit · {answer['ticket']}\nRepo: not named.\nDone when: the gate command passes."
    ]
    assert reply_after(ctx, session="") is None


def test_the_started_reply_goes_only_where_the_ticket_reports() -> None:
    from superagent.domain import Mandate, Strength, Ticket, TicketStatus, Trigger
    from superagent.plugin import started_goes_in_reply

    mandate = Mandate("v", "g", Strength.EXPLICIT, "a", "discord", "t")
    ticket = Ticket("tk-1", "g", mandate, "discord:c1", TicketStatus.OPEN)
    assert started_goes_in_reply(Trigger("t", "discord", "", "a", "x", "discord:c1", True), ticket)
    # A CLI turn answers in the terminal; its ticket reports to the [sources.cli] notify channel.
    assert not started_goes_in_reply(Trigger("t", "cli", "", "a", "x", "", True), ticket)
    assert not started_goes_in_reply(Trigger("t", "discord", "", "a", "x", "discord:other", True), ticket)


def test_the_reply_hook_holds_the_stream_and_is_inert_outside_the_router(tmp_path: Path) -> None:
    ctx, _ = router_home(tmp_path)
    assert ctx.hook_options["transform_llm_output"] == {"hold_stream": True}
    assert context("worker", tmp_path).hooks["transform_llm_output"](response_text="hi", session_id="s") is None


def test_done_when_falls_back_to_the_gate(tmp_path: Path) -> None:
    ctx, _ = router_home(tmp_path)
    dispatch(ctx, admitted_trigger(ctx), title="Push retry limit")
    assert reply_after(ctx).endswith("\nRepo: not named.\nDone when: the gate command passes.")


def test_done_when_is_capped_at_12_words(tmp_path: Path) -> None:
    ctx, _ = router_home(tmp_path)
    long = "all the CI checks on the pull request pass and the review is approved"
    assert (
        "done_when has 14 words; the limit is 12."
        in dispatch(ctx, admitted_trigger(ctx), title="X", done_when=long)["error"]
    )


def queued_replies(home: Path) -> list[str]:
    return [message.text for message in Store(home / DB_FILENAME).pending_outbox() if message.ticket_id == "router"]


def wait_for(predicate: Any, seconds: float = 5.0) -> None:
    deadline = time.time() + seconds
    while not predicate() and time.time() < deadline:
        time.sleep(0.05)


def test_status_from_leo_is_answered_by_code_with_the_status_template(tmp_path: Path) -> None:
    ctx, sent = router_home(tmp_path)
    dispatch(ctx, admitted_trigger(ctx), title="Push retry limit")
    decision = inbound(ctx, text="Status?", message_id="2")
    assert decision["action"] == "skip"
    assert offered(ctx, "Status?") is None
    wait_for(lambda: len(sent_texts(sent)) == 1)
    [reply] = sent_texts(sent)
    assert reply == "1 ticket running. None needs you.\n• Push retry limit: running for 0 min."
    assert style.check_message(reply, max_lines=style.MAX_STATUS_LINES) == []


def test_status_from_a_stranger_is_not_answered(tmp_path: Path) -> None:
    ctx, sent = router_home(tmp_path)
    assert inbound(ctx, text="status", user_id="u-mallory")["action"] == "skip"
    time.sleep(0.2)
    assert sent_texts(sent) == [] and queued_replies(tmp_path) == []


def test_details_returns_the_stored_details_and_checks(tmp_path: Path) -> None:
    ctx, sent = router_home(tmp_path)
    ticket_id = dispatch(ctx, admitted_trigger(ctx), title="Push retry limit")["ticket"]
    store = Store(tmp_path / DB_FILENAME)
    assignment = store.running_assignments()[0]
    identity = tmp_path / WORKDIRS_DIRNAME / ticket_id / assignment.id / IDENTITY_FILENAME
    token = json.loads(identity.read_text())["token"]
    fields = ReportFields(
        outcome="All 3 CI jobs pass.",
        checks=(Check("relay-guard", True),),
        details="Run 42756344802 on macos-26-arm64: relay-guard 364 passed.",
    )
    store.add_report(assignment.id, token, ReportState.READY, "x", fields=fields)
    assert inbound(ctx, text=f"details {ticket_id}", message_id="3")["action"] == "skip"
    wait_for(lambda: len(sent_texts(sent)) >= 1)
    reply = "\n".join(sent_texts(sent))
    assert reply.startswith(f"Details · Push retry limit · {ticket_id}\n")
    assert "Run 42756344802 on macos-26-arm64: relay-guard 364 passed." in reply
    assert "Checks: relay-guard ✓" in reply


def test_details_of_an_unknown_ticket_says_so(tmp_path: Path) -> None:
    ctx, sent = router_home(tmp_path)
    assert inbound(ctx, text="details tk-00000000", message_id="4")["action"] == "skip"
    wait_for(lambda: len(sent_texts(sent)) == 1)
    assert sent_texts(sent) == ["No ticket tk-00000000."]


def test_router_and_watchdog_prompts_carry_the_how_to_write_block(tmp_path: Path) -> None:
    for role in ("router", "watchdog"):
        section = context(role, tmp_path).sections["superagent.role"]({})
        assert style.HOW_TO_WRITE in section
    router_prompt = context("router", tmp_path).sections["superagent.role"]({})
    assert "[SILENT]" in router_prompt


def test_how_to_write_has_three_example_pairs_from_real_messages() -> None:
    assert style.HOW_TO_WRITE.count("Bad:") == 3 and style.HOW_TO_WRITE.count("Good:") == 3
    assert "tk-ee69ce72" in style.HOW_TO_WRITE
