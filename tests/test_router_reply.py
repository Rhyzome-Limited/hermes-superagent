"""Router chat replies: the router prompt sets the limits, and a reply that breaks a rule gets one rewrite."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from superagent import style
from superagent.judges import rewrite_reply
from superagent.plugin import ROUTER_PROMPT

from .test_plain_output import admitted_trigger, dispatch, reply_after, router_home

# A real router reply to "What can you do?" on 2026-11-10 (router state.db row 245): 8 lines with blank ones.
REAL_REPLY = (
    "I start workers and send their reports. I do not run code myself.\n\n"
    "- Send a task in a repo and I start a worker on it.\n"
    "- Reply to a running task and I pass your message to the worker.\n"
    "- Ask to stop a task and I cancel it.\n"
    "- Ask for status and I list open tickets.\n\n"
    'Code sends the Started, Done, Needs you and Failed messages. Try: "add a README line to superagent-sandbox".'
)
PLAIN = (
    "I start workers and pass on their reports. I never run code myself.\n"
    "Send me a task with its repo and gate.\n"
    'Try: "add a README line to superagent-sandbox".'
)
# Ticket ids, PR numbers, backticked commands and quoted replies are what Leo acts on, so a rewrite keeps them exactly.
PARKED = (
    "The plan for tk-1a2b3c4d is in PR #12, and it changes `ops/retry.py`, which tk-5e6f7a8b also touches, "
    'so reply "approve tk-1a2b3c4d" or say what to change.'
)
PARKED_REWRITE = (
    "The plan for tk-1a2b3c4d is in PR #12.\n"
    "It changes `ops/retry.py`, which tk-5e6f7a8b also touches.\n"
    'Reply "approve tk-1a2b3c4d" or say what to change.'
)


@dataclass
class Result:
    parsed: object


@dataclass
class RecordingLlm:
    answer: object = None
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def complete_structured(self, **kwargs: Any) -> Result:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return Result(self.answer)


def test_the_real_reply_breaks_the_line_rule() -> None:
    assert "message-lines" in {problem.rule for problem in style.check_message(REAL_REPLY)}


def test_a_reply_that_breaks_a_rule_is_rewritten_once_with_its_problems() -> None:
    llm = RecordingLlm({"reply": PLAIN})
    assert rewrite_reply(llm, REAL_REPLY) == PLAIN
    [call] = llm.calls
    assert "the message has 8 lines; the limit is 6" in call["instructions"]
    assert call["timeout"] < 30  # Hermes abandons a transform_llm_output callback after 30 s


def test_a_plain_reply_is_left_alone() -> None:
    llm = RecordingLlm({"reply": "unused"})
    assert rewrite_reply(llm, PLAIN) is None
    assert rewrite_reply(llm, "[SILENT]") is None
    assert llm.calls == []


def test_a_rewrite_that_still_breaks_a_rule_keeps_the_original() -> None:
    assert rewrite_reply(RecordingLlm({"reply": REAL_REPLY}), REAL_REPLY) is None
    assert rewrite_reply(RecordingLlm({"wrong": "shape"}), REAL_REPLY) is None


def test_a_rewrite_that_keeps_what_leo_acts_on_is_used() -> None:
    assert style.check_message(PARKED) and not style.check_message(PARKED_REWRITE)
    assert rewrite_reply(RecordingLlm({"reply": PARKED_REWRITE}), PARKED) == PARKED_REWRITE


def test_a_rewrite_that_drops_the_approve_command_keeps_the_original() -> None:
    dropped = PARKED_REWRITE.replace('Reply "approve tk-1a2b3c4d"', "Approve it")
    assert not style.check_message(dropped)
    assert rewrite_reply(RecordingLlm({"reply": dropped}), PARKED) is None


@pytest.mark.parametrize(
    ("kept", "changed"),
    [("tk-5e6f7a8b", "another ticket"), ("PR #12", "the PR"), ("`ops/retry.py`", "ops/retry.py")],
)
def test_a_rewrite_that_changes_a_ticket_id_pr_number_or_command_keeps_the_original(kept: str, changed: str) -> None:
    rewrite = PARKED_REWRITE.replace(kept, changed)
    assert not style.check_message(rewrite)
    assert rewrite_reply(RecordingLlm({"reply": rewrite}), PARKED) is None


def test_a_failing_model_keeps_the_original() -> None:
    assert rewrite_reply(RecordingLlm(error=TimeoutError("slow")), REAL_REPLY) is None


def test_the_router_hook_rewrites_its_own_reply(tmp_path: Path) -> None:
    ctx, _ = router_home(tmp_path)
    ctx.llm = RecordingLlm({"reply": PLAIN})  # type: ignore[assignment]
    assert reply_after(ctx, text=REAL_REPLY) == PLAIN


def test_the_started_message_wins_and_costs_no_model_call(tmp_path: Path) -> None:
    ctx, _ = router_home(tmp_path)
    llm = RecordingLlm({"reply": PLAIN})
    ctx.llm = llm  # type: ignore[assignment]
    dispatch(ctx, admitted_trigger(ctx), title="Push retry limit")
    assert reply_after(ctx, text=REAL_REPLY).startswith("▶️ Started · Push retry limit · tk-")
    assert llm.calls == []


def test_the_router_prompt_sets_the_reply_limits_and_the_facts_it_lacked() -> None:
    assert "max 6 lines, no blank lines, at most 3 bullets" in ROUTER_PROMPT
    assert "approve <ticket>" in ROUTER_PROMPT and "superagent_forward" in ROUTER_PROMPT
    assert "watchdog" in ROUTER_PROMPT and "details <ticket>" in ROUTER_PROMPT
