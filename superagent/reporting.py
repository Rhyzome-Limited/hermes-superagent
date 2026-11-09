from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .config import DB_FILENAME, WORKDIRS_DIRNAME
from .domain import ReportState
from .store import Store
from .worker import IDENTITY_FILENAME


@dataclass(frozen=True)
class WorkerIdentity:
    assignment_id: str
    token: str
    ticket_id: str
    db_path: Path
    attempt: int


class NotAWorkerError(RuntimeError):
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
    identity: WorkerIdentity, state: ReportState, summary: str, question: str = "", scope_class: str = ""
) -> dict[str, object]:
    store = Store(identity.db_path)
    store.add_report(identity.assignment_id, identity.token, state, summary, question, scope_class)
    inputs = [
        {"from": label, "from_allowed_person": trusted, "content": text}
        for label, text, trusted in store.take_inputs(identity.ticket_id)
    ]
    result: dict[str, object] = {"reported": state.value, "inputs": inputs}
    if scope_class:
        result["scope_class"] = store.get_ticket(identity.ticket_id).scope_class
    return result


def record_tool_activity(identity: WorkerIdentity, tool_name: str, args: object, result: object) -> None:
    payload = json.dumps([tool_name, args, str(result)], sort_keys=True, default=str)
    fingerprint = hashlib.sha256(payload.encode()).hexdigest()[:16]
    Store(identity.db_path).record_activity(identity.assignment_id, identity.token, fingerprint)
