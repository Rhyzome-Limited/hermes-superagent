from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Admission(StrEnum):
    ACT = "act"
    CANDIDATE = "candidate"
    IGNORE = "ignore"


class Strength(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"


class ReportState(StrEnum):
    PROGRESS = "progress"
    READY = "ready"
    BLOCKED = "blocked"
    FAILED = "failed"
    DROPPED = "dropped"


FINAL_REPORT_STATES = frozenset({ReportState.READY, ReportState.FAILED, ReportState.DROPPED})
HUMAN_FACING_REPORT_STATES = frozenset({ReportState.READY, ReportState.BLOCKED, ReportState.FAILED})


class TicketStatus(StrEnum):
    OPEN = "open"
    WAITING = "waiting"
    QUEUED = "queued"
    ENDED = "ended"


class Loop(StrEnum):
    KASTLE_BUILD = "kastle-build-loop"
    CLAUDE_ASSISTED = "claude-assisted-loop"
    BUILD = "build-loop"


# Work that reaches a ticket without the router choosing, such as an act-mode rule or `superagent ingest`.
DEFAULT_LOOP = Loop.CLAUDE_ASSISTED
# Live workers at once, the cap the builder's claude-multi-loop kept (task 2.9).
DEFAULT_MAX_RUNNING = 4


class ScopeClass(StrEnum):
    IN_SCOPE = "in-scope"
    LEO_GATED = "leo-gated"


class AssignmentStatus(StrEnum):
    RUNNING = "running"
    ENDED = "ended"


class Verdict(StrEnum):
    ON_TRACK = "on_track"
    STUCK = "stuck"
    DRIFTING = "drifting"


class RepairAction(StrEnum):
    NONE = "none"
    NUDGE = "nudge"
    RESTART = "restart"
    ESCALATE = "escalate"
    PARK = "park"


@dataclass(frozen=True)
class Trigger:
    id: str
    source: str
    thread: str
    actor: str
    text: str
    channel: str = ""
    addressed: bool = False
    is_bot: bool = False


@dataclass(frozen=True)
class Mandate:
    policy_version: str
    scope: str
    strength: Strength
    actor: str
    source: str
    trigger_id: str


@dataclass(frozen=True)
class Ticket:
    id: str
    goal: str
    mandate: Mandate
    channel: str
    status: TicketStatus
    loop: str = ""
    repo: str = ""
    scope_class: str = ""
    title: str = ""
    done_when: str = ""
    created_at: float = 0.0


@dataclass(frozen=True)
class Assignment:
    id: str
    ticket_id: str
    attempt: int
    status: AssignmentStatus
    pid: int | None
    started_at: float


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool


@dataclass(frozen=True)
class ReportFields:
    """Report schema v2 (tuning plan A.1). Every field but lessons and details can reach chat."""

    outcome: str = ""
    changes: tuple[str, ...] = ()
    checks: tuple[Check, ...] = ()
    links: tuple[str, ...] = ()
    next_for_leo: str = ""
    topic: str = ""
    lessons: tuple[str, ...] = ()
    details: str = ""


@dataclass(frozen=True)
class Report:
    assignment_id: str
    ticket_id: str
    state: ReportState
    summary: str
    question: str
    at: float
    # None for a report stored before schema v2: its free summary is all there is.
    fields: ReportFields | None = None
    style_fail: bool = False


@dataclass(frozen=True)
class DigestItem:
    ticket_id: str
    goal: str
    text: str
