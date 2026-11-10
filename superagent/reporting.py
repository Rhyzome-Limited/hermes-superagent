from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path

from .config import DB_FILENAME, WORKDIRS_DIRNAME
from .domain import HUMAN_FACING_REPORT_STATES, ReportFields, ReportState
from .store import Store
from .style import Problem, check_field
from .worker import IDENTITY_FILENAME

logger = logging.getLogger(__name__)

# A worker may rewrite a refused report twice. The third goes through, with only code-written text in chat.
MAX_STYLE_REFUSALS = 2
MAX_CHANGES = 3
OUTCOME_WORDS = 20
CHANGE_WORDS = 15
QUESTION_WORDS = 25
TOPIC_WORDS = 4
CHECK_NAME_WORDS = 5


@dataclass(frozen=True)
class WorkerIdentity:
    assignment_id: str
    token: str
    ticket_id: str
    db_path: Path
    attempt: int


class NotAWorkerError(RuntimeError):
    pass


class StyleError(ValueError):
    pass


def identity_from_workdir(workdir: Path) -> WorkerIdentity:
    try:
        data = json.loads((workdir / IDENTITY_FILENAME).read_text(encoding="utf-8"))
        assignment_id, ticket_id, token, attempt = data["assignment"], data["ticket"], data["token"], data["attempt"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise NotAWorkerError(f"{workdir} is not a superagent worker directory: {error}") from error
    in_place = len(workdir.parents) >= 3 and workdir.parents[1].name == WORKDIRS_DIRNAME
    if not in_place or workdir.name != assignment_id or workdir.parent.name != ticket_id:
        raise NotAWorkerError(f"{workdir} does not match the identity it holds")
    return WorkerIdentity(assignment_id, token, ticket_id, workdir.parents[2] / DB_FILENAME, int(attempt))


def submit_report(
    identity: WorkerIdentity,
    state: ReportState,
    summary: str,
    question: str = "",
    scope_class: str = "",
    fields: ReportFields | None = None,
) -> dict[str, object]:
    fields = fields or ReportFields()
    if summary and not fields.details:
        fields = replace(fields, details=summary)  # summary is the old name of details (tuning plan A.1)
    store = Store(identity.db_path)
    problems = check_report(state, fields, question) if state in HUMAN_FACING_REPORT_STATES else []
    if problems:
        refusals = store.refuse_style(identity.assignment_id, identity.token, MAX_STYLE_REFUSALS)
        if refusals is not None:
            last = (
                " This is the last try: the next report goes through as it is."
                if refusals == MAX_STYLE_REFUSALS
                else ""
            )
            raise StyleError(
                f"Report refused, Leo would read it in chat. {' '.join(problems)} Fix each point and report again."
                f"{last}"
            )
        logger.warning(
            "style_fail: %s %s reported %s with: %s",
            identity.ticket_id,
            identity.assignment_id,
            state.value,
            " ".join(problems),
        )
    machine_text = "\n".join(part for part in (fields.outcome, fields.details) if part)
    store.add_report(
        identity.assignment_id,
        identity.token,
        state,
        machine_text,
        question,
        scope_class,
        fields=fields,
        style_fail=bool(problems),
    )
    inputs = [
        {"from": label, "from_allowed_person": trusted, "content": text}
        for label, text, trusted in store.take_inputs(identity.ticket_id)
    ]
    result: dict[str, object] = {"reported": state.value, "inputs": inputs}
    if scope_class:
        result["scope_class"] = store.get_ticket(identity.ticket_id).scope_class
    return result


def check_report(state: ReportState, fields: ReportFields, question: str) -> list[str]:
    """Every field that reaches chat, by the A.2 rules and its own limit. Lessons and details never reach chat."""
    problems: list[Problem] = []
    if not fields.outcome.strip():
        problems.append(Problem("missing", "outcome is missing. Write what is true now in plain words, max 20 words."))
    problems += check_field("outcome", fields.outcome, OUTCOME_WORDS)
    if len(fields.changes) > MAX_CHANGES:
        problems.append(Problem("items", f"changes has {len(fields.changes)} items; the limit is {MAX_CHANGES}."))
    for change in fields.changes:
        problems += check_field("a change", change, CHANGE_WORDS)
    for check in fields.checks:
        problems += check_field("a check name", check.name, CHECK_NAME_WORDS)
    problems += check_field("next_for_leo", fields.next_for_leo, OUTCOME_WORDS)
    problems += check_field("topic", fields.topic, TOPIC_WORDS)
    if state is ReportState.BLOCKED:
        if not question.strip():
            problems.append(Problem("missing", "question is missing. Ask the one decision Leo must make."))
        problems += check_field("question", question, QUESTION_WORDS)
    return [_sentence(problem.message) for problem in problems]


def _sentence(text: str) -> str:
    return text if text.endswith(".") else f"{text}."


def record_tool_activity(identity: WorkerIdentity, tool_name: str, args: object, result: object) -> None:
    payload = json.dumps([tool_name, args, str(result)], sort_keys=True, default=str)
    fingerprint = hashlib.sha256(payload.encode()).hexdigest()[:16]
    Store(identity.db_path).record_activity(identity.assignment_id, identity.token, fingerprint)
