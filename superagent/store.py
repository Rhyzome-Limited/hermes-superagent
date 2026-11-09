from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .domain import (
    FINAL_REPORT_STATES,
    Admission,
    Assignment,
    AssignmentStatus,
    DigestItem,
    Loop,
    Mandate,
    RepairAction,
    Report,
    ReportState,
    ScopeClass,
    Strength,
    Ticket,
    TicketStatus,
    Trigger,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY, text TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL, notified_at REAL
);
CREATE TABLE IF NOT EXISTS triggers (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, thread TEXT NOT NULL, actor TEXT NOT NULL, text TEXT NOT NULL,
    channel TEXT NOT NULL, addressed INTEGER NOT NULL, is_bot INTEGER NOT NULL,
    admission TEXT NOT NULL, reason TEXT NOT NULL, policy_version TEXT NOT NULL, received_at REAL NOT NULL,
    assessed_at REAL, assessment TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY, goal TEXT NOT NULL, mandate TEXT NOT NULL, channel TEXT NOT NULL,
    status TEXT NOT NULL, created_at REAL NOT NULL, trigger_id TEXT NOT NULL UNIQUE, resumed_at REAL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    thread TEXT NOT NULL, ticket_id TEXT NOT NULL, PRIMARY KEY (thread, ticket_id)
);
CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, attempt INTEGER NOT NULL, token_hash TEXT NOT NULL,
    status TEXT NOT NULL, pid INTEGER, started_at REAL NOT NULL, ended_at REAL, reaped_at REAL
);
CREATE TABLE IF NOT EXISTS reports (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id TEXT NOT NULL, ticket_id TEXT NOT NULL,
    state TEXT NOT NULL, summary TEXT NOT NULL, question TEXT NOT NULL, at REAL NOT NULL, relayed_at REAL
);
CREATE TABLE IF NOT EXISTS inputs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL, label TEXT NOT NULL, text TEXT NOT NULL,
    trusted INTEGER NOT NULL, delivered_at REAL
);
CREATE TABLE IF NOT EXISTS outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL, channel TEXT NOT NULL, text TEXT NOT NULL,
    sent_at REAL, attempts INTEGER NOT NULL DEFAULT 0, failed_at REAL
);
CREATE TABLE IF NOT EXISTS assessment_failures (
    trigger_id TEXT PRIMARY KEY, failures INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS activities (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id TEXT NOT NULL, fingerprint TEXT NOT NULL, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS watchdog_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id TEXT NOT NULL, ticket_id TEXT NOT NULL,
    verdict TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, at REAL NOT NULL, relayed_at REAL
);
"""


BUSY_TIMEOUT_SECONDS = 5
MAX_REPORT_CHARS = 2000
OFFERED = "offered to router"
_LIVE_STATUSES = (TicketStatus.OPEN.value, TicketStatus.WAITING.value)


class StaleAssignmentError(PermissionError):
    pass


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_MIGRATIONS = (
    "ALTER TABLE outbox ADD COLUMN next_attempt_at REAL",
    "ALTER TABLE outbox ADD COLUMN drop_reason TEXT",
    "UPDATE outbox SET failed_at = NULL WHERE sent_at IS NULL AND failed_at IS NOT NULL AND drop_reason IS NULL",
    "ALTER TABLE tickets ADD COLUMN loop TEXT",
    "ALTER TABLE tickets ADD COLUMN repo TEXT",
    "ALTER TABLE tickets ADD COLUMN scope_class TEXT",
)
SEND_RETRY_BASE_SECONDS = 60.0
SEND_RETRY_CAP_SECONDS = 1800.0
MESSAGE_LIMIT = 1900
ALERT_TICKET = "alert"
SEND_FIRST_RETRY_SECONDS = 30.0


@dataclass(frozen=True)
class OutboxMessage:
    seq: int
    ticket_id: str
    channel: str
    text: str
    attempts: int
    due: bool


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    text = text.replace("\r\n", "\n")
    if len(text) <= limit:
        return [text]
    room = limit - len("(999/999) ")
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > room:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:room])
            line = line[room:]
        if len(current) + len(line) > room:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return [
        f"({index}/{len(chunks)}) {chunk[:-1] if chunk.endswith(chr(10)) else chunk}"
        for index, chunk in enumerate(chunks, 1)
    ]


def _queue(connection: sqlite3.Connection, ticket_id: str, channel: str, text: str) -> None:
    connection.executemany(
        "INSERT INTO outbox (ticket_id, channel, text) VALUES (?,?,?)",
        [(ticket_id, channel, part) for part in split_message(text)],
    )


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            for statement in _MIGRATIONS[version:]:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {len(_MIGRATIONS)}")
            connection.execute("COMMIT")
        finally:
            connection.close()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def record_trigger(self, trigger: Trigger, admission: Admission, reason: str, policy_version: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO triggers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                (
                    trigger.id,
                    trigger.source,
                    trigger.thread,
                    trigger.actor,
                    trigger.text,
                    trigger.channel,
                    int(trigger.addressed),
                    int(trigger.is_bot),
                    admission.value,
                    reason,
                    policy_version,
                    time.time(),
                ),
            )
            return cursor.rowcount == 1

    def admission_of(self, trigger_id: str) -> Admission | None:
        with self._connect() as connection:
            row = connection.execute("SELECT admission FROM triggers WHERE id = ?", (trigger_id,)).fetchone()
        return Admission(row["admission"]) if row else None

    def get_trigger(self, trigger_id: str) -> Trigger | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM triggers WHERE id = ?", (trigger_id,)).fetchone()
        return _trigger_from_row(row) if row else None

    def create_ticket(
        self, goal: str, mandate: Mandate, channel: str, thread: str, loop: str = "", repo: str = ""
    ) -> tuple[Ticket, bool]:
        ticket_id = f"tk-{secrets.token_hex(4)}"
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO tickets "
                "(id, goal, mandate, channel, status, created_at, trigger_id, loop, repo) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ticket_id,
                    goal,
                    json.dumps(asdict(mandate)),
                    channel,
                    TicketStatus.OPEN.value,
                    time.time(),
                    mandate.trigger_id,
                    loop,
                    repo,
                ),
            )
            if cursor.rowcount == 1:
                if thread:
                    connection.execute("INSERT INTO subscriptions VALUES (?,?)", (thread, ticket_id))
                return Ticket(ticket_id, goal, mandate, channel, TicketStatus.OPEN, loop, repo), True
            row = connection.execute("SELECT * FROM tickets WHERE trigger_id = ?", (mandate.trigger_id,)).fetchone()
        return _ticket_from_row(row), False

    def ticket_for_trigger(self, trigger_id: str) -> Ticket | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tickets WHERE trigger_id = ?", (trigger_id,)).fetchone()
        return _ticket_from_row(row) if row else None

    def get_ticket(self, ticket_id: str) -> Ticket:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise KeyError(f"no such ticket: {ticket_id}")
        return _ticket_from_row(row)

    def list_tickets(self, status: TicketStatus | None = None) -> list[Ticket]:
        query = "SELECT * FROM tickets"
        args: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            args = (status.value,)
        with self._connect() as connection:
            rows = connection.execute(query + " ORDER BY created_at", args).fetchall()
        return [_ticket_from_row(row) for row in rows]

    def live_tickets(self) -> list[Ticket]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tickets WHERE status IN (?,?) ORDER BY created_at", _LIVE_STATUSES
            ).fetchall()
        return [_ticket_from_row(row) for row in rows]

    def set_ticket_status(self, ticket_id: str, status: TicketStatus) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE tickets SET status = ? WHERE id = ?", (status.value, ticket_id))

    def end_ticket(self, ticket_id: str) -> None:
        self.set_ticket_status(ticket_id, TicketStatus.ENDED)

    def orphaned_tickets(self, older_than_seconds: float) -> list[Ticket]:
        cutoff = time.time() - older_than_seconds
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tickets t WHERE t.status = ? AND t.created_at <= ? AND NOT EXISTS "
                "(SELECT 1 FROM assignments a WHERE a.ticket_id = t.id AND a.status = ?) AND NOT EXISTS "
                "(SELECT 1 FROM assignments a WHERE a.ticket_id = t.id AND a.ended_at > ?) ORDER BY t.created_at",
                (TicketStatus.OPEN.value, cutoff, AssignmentStatus.RUNNING.value, cutoff),
            ).fetchall()
        return [_ticket_from_row(row) for row in rows]

    def subscribers(self, thread: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT s.ticket_id FROM subscriptions s JOIN tickets t ON t.id = s.ticket_id "
                "WHERE s.thread = ? AND t.status IN (?,?)",
                (thread, *_LIVE_STATUSES),
            ).fetchall()
        return [row["ticket_id"] for row in rows]

    def create_assignment(self, ticket_id: str) -> tuple[Assignment, str] | None:
        with self._connect() as connection:
            status = connection.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            if status is None or status["status"] != TicketStatus.OPEN.value:
                return None
            return _insert_assignment(connection, ticket_id)

    def resume_waiting_ticket(self, ticket_id: str) -> tuple[Assignment, str] | None:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tickets SET status = ?, resumed_at = ? WHERE id = ? AND status = ?",
                (TicketStatus.OPEN.value, now, ticket_id, TicketStatus.WAITING.value),
            )
            if cursor.rowcount != 1:
                return None
            return _insert_assignment(connection, ticket_id)

    def set_assignment_pid(self, assignment_id: str, pid: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE assignments SET pid = ? WHERE id = ?", (pid, assignment_id))

    def end_assignment(self, assignment_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE assignments SET status = ?, ended_at = ? WHERE id = ? AND status = ?",
                (AssignmentStatus.ENDED.value, time.time(), assignment_id, AssignmentStatus.RUNNING.value),
            )
            return cursor.rowcount == 1

    def park_silent_exit(self, assignment_id: str, summary: str, question: str) -> bool:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute("SELECT ticket_id FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
            cursor = connection.execute(
                "UPDATE assignments SET status = ?, ended_at = ? WHERE id = ? AND status = ?",
                (AssignmentStatus.ENDED.value, now, assignment_id, AssignmentStatus.RUNNING.value),
            )
            if row is None or cursor.rowcount != 1:
                return False
            connection.execute(
                "INSERT INTO reports (assignment_id, ticket_id, state, summary, question, at) VALUES (?,?,?,?,?,?)",
                (assignment_id, row["ticket_id"], ReportState.BLOCKED.value, summary, question, now),
            )
            connection.execute(
                "UPDATE tickets SET status = ? WHERE id = ? AND status = ?",
                (TicketStatus.WAITING.value, row["ticket_id"], TicketStatus.OPEN.value),
            )
            return True

    def assignment_is_current(self, assignment_id: str, token: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT token_hash, status FROM assignments WHERE id = ?", (assignment_id,)
            ).fetchone()
        return (
            row is not None
            and secrets.compare_digest(row["token_hash"], hash_token(token))
            and row["status"] == AssignmentStatus.RUNNING.value
        )

    def assignments_to_reap(self, grace_seconds: float) -> list[Assignment]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM assignments WHERE status = ? AND reaped_at IS NULL AND pid IS NOT NULL "
                "AND ended_at <= ?",
                (AssignmentStatus.ENDED.value, time.time() - grace_seconds),
            ).fetchall()
        return [_assignment_from_row(row) for row in rows]

    def mark_reaped(self, assignment_id: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE assignments SET reaped_at = ? WHERE id = ?", (time.time(), assignment_id))

    def get_assignment(self, assignment_id: str) -> Assignment:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
        if row is None:
            raise KeyError(f"no such assignment: {assignment_id}")
        return _assignment_from_row(row)

    def running_assignments(self) -> list[Assignment]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM assignments WHERE status = ? ORDER BY started_at", (AssignmentStatus.RUNNING.value,)
            ).fetchall()
        return [_assignment_from_row(row) for row in rows]

    def add_report(
        self,
        assignment_id: str,
        token: str,
        state: ReportState,
        summary: str,
        question: str = "",
        scope_class: str = "",
    ) -> Report:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
            if row is None or not secrets.compare_digest(row["token_hash"], hash_token(token)):
                raise StaleAssignmentError(f"bad credentials for assignment {assignment_id}")
            if row["status"] != AssignmentStatus.RUNNING.value:
                raise StaleAssignmentError(f"assignment {assignment_id} has ended, report refused")
            if scope_class:
                _set_scope_class(connection, row["ticket_id"], scope_class)
            summary = summary[:MAX_REPORT_CHARS]
            question = question[:MAX_REPORT_CHARS]
            connection.execute(
                "INSERT INTO reports (assignment_id, ticket_id, state, summary, question, at) VALUES (?,?,?,?,?,?)",
                (assignment_id, row["ticket_id"], state.value, summary, question, now),
            )
            if state in FINAL_REPORT_STATES or state is ReportState.BLOCKED:
                connection.execute(
                    "UPDATE assignments SET status = ?, ended_at = ? WHERE id = ?",
                    (AssignmentStatus.ENDED.value, now, assignment_id),
                )
                status = TicketStatus.WAITING if state is ReportState.BLOCKED else TicketStatus.ENDED
                connection.execute("UPDATE tickets SET status = ? WHERE id = ?", (status.value, row["ticket_id"]))
        return Report(assignment_id, row["ticket_id"], state, summary, question, now)

    def reports_for(self, assignment_id: str, limit: int = 20) -> list[Report]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reports WHERE assignment_id = ? ORDER BY seq DESC LIMIT ?", (assignment_id, limit)
            ).fetchall()
        return [_report_from_row(row) for row in reversed(rows)]

    def ticket_reports(self, ticket_id: str) -> list[Report]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM reports WHERE ticket_id = ? ORDER BY seq", (ticket_id,)).fetchall()
        return [_report_from_row(row) for row in rows]

    def move_to_outbox(
        self,
        render_report: Callable[[Report, str], str | None],
        render_escalation: Callable[[str, str, str], str],
    ) -> None:
        now = time.time()
        with self._connect() as connection:
            reports = connection.execute(
                "SELECT r.*, t.channel, t.goal FROM reports r JOIN tickets t ON t.id = r.ticket_id "
                "WHERE r.relayed_at IS NULL ORDER BY r.seq"
            ).fetchall()
            for row in reports:
                text = render_report(_report_from_row(row), row["goal"])
                if text is not None:
                    _queue(connection, row["ticket_id"], row["channel"], text)
                connection.execute("UPDATE reports SET relayed_at = ? WHERE seq = ?", (now, row["seq"]))
            escalations = connection.execute(
                "SELECT e.seq, e.ticket_id, e.reason, t.channel, t.goal FROM watchdog_events e "
                "JOIN tickets t ON t.id = e.ticket_id WHERE e.relayed_at IS NULL AND e.action = ? ORDER BY e.seq",
                (RepairAction.ESCALATE.value,),
            ).fetchall()
            for row in escalations:
                text = render_escalation(row["reason"], row["ticket_id"], row["goal"])
                _queue(connection, row["ticket_id"], row["channel"], text)
                connection.execute("UPDATE watchdog_events SET relayed_at = ? WHERE seq = ?", (now, row["seq"]))

    def final_reports_since(self, since: float) -> list[tuple[DigestItem, ReportState]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT r.ticket_id, r.state, r.summary, t.goal FROM reports r JOIN tickets t ON t.id = r.ticket_id "
                "WHERE r.at >= ? AND r.state IN (?,?,?) ORDER BY r.seq",
                (since, ReportState.READY.value, ReportState.FAILED.value, ReportState.DROPPED.value),
            ).fetchall()
        return [(DigestItem(row["ticket_id"], row["goal"], row["summary"]), ReportState(row["state"])) for row in rows]

    def escalations_since(self, since: float) -> list[DigestItem]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT e.ticket_id, e.reason, t.goal FROM watchdog_events e JOIN tickets t ON t.id = e.ticket_id "
                "WHERE e.at >= ? AND e.action = ? ORDER BY e.seq",
                (since, RepairAction.ESCALATE.value),
            ).fetchall()
        return [DigestItem(row["ticket_id"], row["goal"], row["reason"]) for row in rows]

    def assessments_since(self, since: float, prefix: str) -> list[tuple[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT assessment, source FROM triggers WHERE assessed_at >= ? AND substr(assessment, 1, ?) = ? "
                "ORDER BY assessed_at",
                (since, len(prefix), prefix),
            ).fetchall()
        return [(row["assessment"], row["source"]) for row in rows]

    def admission_counts_since(self, since: float, admission: Admission) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT source FROM triggers WHERE received_at >= ? AND admission = ?", (since, admission.value)
            ).fetchall()
        return [row["source"] for row in rows]

    def queue_message(self, ticket_id: str, channel: str, text: str) -> None:
        with self._connect() as connection:
            _queue(connection, ticket_id, channel, text)

    def sync_alerts(
        self, active: Mapping[str, str], alert_after: float, channel: str, immediate: frozenset[str] = frozenset()
    ) -> tuple[list[str], list[str]]:
        now = time.time()
        raised: list[str] = []
        resolved: list[str] = []
        with self._connect() as connection:
            known = {row["key"]: row for row in connection.execute("SELECT * FROM alerts").fetchall()}
            for key, row in known.items():
                if key in active or now - row["last_seen"] < alert_after:
                    continue
                connection.execute("DELETE FROM alerts WHERE key = ?", (key,))
                if row["notified_at"] is not None:
                    _queue(connection, ALERT_TICKET, channel, f"Resolved: {row['text']}")
                    resolved.append(row["text"])
            for key, text in active.items():
                row = known.get(key)
                if row is None:
                    connection.execute("INSERT INTO alerts VALUES (?, ?, ?, ?, NULL)", (key, text, now, now))
                    first_seen, notified = now, False
                else:
                    connection.execute("UPDATE alerts SET text = ?, last_seen = ? WHERE key = ?", (text, now, key))
                    first_seen, notified = row["first_seen"], row["notified_at"] is not None
                if channel and not notified and (key in immediate or now - first_seen >= alert_after):
                    connection.execute("UPDATE alerts SET notified_at = ? WHERE key = ?", (now, key))
                    _queue(connection, ALERT_TICKET, channel, f"Alert: {text}")
                    raised.append(text)
        return raised, resolved

    def raise_alert_now(self, key: str, text: str, channel: str) -> bool:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute("SELECT notified_at FROM alerts WHERE key = ?", (key,)).fetchone()
            if row is None:
                connection.execute("INSERT INTO alerts VALUES (?, ?, ?, ?, NULL)", (key, text, now, now))
            else:
                connection.execute("UPDATE alerts SET text = ?, last_seen = ? WHERE key = ?", (text, now, key))
            if not channel or (row is not None and row["notified_at"] is not None):
                return False
            connection.execute("UPDATE alerts SET notified_at = ? WHERE key = ?", (now, key))
            _queue(connection, ALERT_TICKET, channel, f"Alert: {text}")
        return True

    def active_alerts(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM alerts ORDER BY first_seen").fetchall()
        return [
            {"key": row["key"], "text": row["text"], "since": row["first_seen"], "notified": row["notified_at"]}
            for row in rows
        ]

    def pending_candidates(self, limit: int = -1) -> list[Trigger]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM triggers WHERE admission = ? AND assessed_at IS NULL ORDER BY received_at LIMIT ?",
                (Admission.CANDIDATE.value, limit),
            ).fetchall()
        triggers = [self.get_trigger(row["id"]) for row in rows]
        return [trigger for trigger in triggers if trigger is not None]

    def mark_assessed(self, trigger_id: str, assessment: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE triggers SET assessed_at = ?, assessment = ? WHERE id = ?",
                (time.time(), assessment, trigger_id),
            )

    def assessment_of(self, trigger_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT assessment FROM triggers WHERE id = ?", (trigger_id,)).fetchone()
        return row["assessment"] if row else None

    def add_input(self, ticket_id: str, label: str, text: str, *, trusted: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO inputs (ticket_id, label, text, trusted) VALUES (?,?,?,?)",
                (ticket_id, label, text, int(trusted)),
            )

    def pending_inputs(self, ticket_id: str) -> list[tuple[int, str, str, bool]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT seq, label, text, trusted FROM inputs "
                "WHERE ticket_id = ? AND delivered_at IS NULL ORDER BY seq",
                (ticket_id,),
            ).fetchall()
        return [(row["seq"], row["label"], row["text"], bool(row["trusted"])) for row in rows]

    def mark_inputs_delivered(self, seqs: Sequence[int]) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.executemany("UPDATE inputs SET delivered_at = ? WHERE seq = ?", [(now, seq) for seq in seqs])

    def take_inputs(self, ticket_id: str) -> list[tuple[str, str, bool]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT seq, label, text, trusted FROM inputs "
                "WHERE ticket_id = ? AND delivered_at IS NULL ORDER BY seq",
                (ticket_id,),
            ).fetchall()
            connection.executemany(
                "UPDATE inputs SET delivered_at = ? WHERE seq = ?", [(time.time(), row["seq"]) for row in rows]
            )
        return [(row["label"], row["text"], bool(row["trusted"])) for row in rows]

    def pending_outbox(self) -> list[OutboxMessage]:
        now = time.time()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT seq, ticket_id, channel, text, attempts, next_attempt_at FROM outbox "
                "WHERE sent_at IS NULL AND failed_at IS NULL ORDER BY seq"
            ).fetchall()
        return [
            OutboxMessage(
                row["seq"],
                row["ticket_id"],
                row["channel"],
                row["text"],
                row["attempts"],
                row["next_attempt_at"] is None or row["next_attempt_at"] <= now,
            )
            for row in rows
        ]

    def drop_message(self, seq: int, reason: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE outbox SET failed_at = ?, drop_reason = ? "
                "WHERE seq = ? AND sent_at IS NULL AND failed_at IS NULL",
                (time.time(), reason, seq),
            )
        return cursor.rowcount == 1

    def mark_sent(self, seq: int) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE outbox SET sent_at = ? WHERE seq = ?", (time.time(), seq))

    def is_pending(self, seq: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM outbox WHERE seq = ? AND sent_at IS NULL AND failed_at IS NULL", (seq,)
            ).fetchone()
        return row is not None

    def record_assessment_failure(self, trigger_ids: Sequence[str], give_up_after: int) -> list[str]:
        now = time.time()
        given_up: list[str] = []
        with self._connect() as connection:
            for trigger_id in trigger_ids:
                connection.execute(
                    "INSERT INTO assessment_failures VALUES (?, 1) "
                    "ON CONFLICT(trigger_id) DO UPDATE SET failures = failures + 1",
                    (trigger_id,),
                )
                row = connection.execute(
                    "SELECT failures FROM assessment_failures WHERE trigger_id = ?", (trigger_id,)
                ).fetchone()
                if row["failures"] >= give_up_after:
                    connection.execute(
                        "UPDATE triggers SET assessed_at = ?, assessment = ? WHERE id = ? AND assessed_at IS NULL",
                        (now, "assessment failed", trigger_id),
                    )
                    given_up.append(trigger_id)
        return given_up

    def failed_send_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM outbox WHERE sent_at IS NULL AND failed_at IS NULL AND attempts > 0"
            ).fetchone()
        return int(row["n"])

    def undelivered_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM outbox WHERE sent_at IS NULL AND failed_at IS NULL"
            ).fetchone()
        return int(row["n"])

    def prune_ignored_triggers(self, older_than_seconds: float) -> int:
        cutoff = time.time() - older_than_seconds
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM triggers WHERE admission = ? AND received_at < ?", (Admission.IGNORE.value, cutoff)
            )
            return cursor.rowcount

    def record_send_failure(self, seq: int) -> float:
        with self._connect() as connection:
            connection.execute("UPDATE outbox SET attempts = attempts + 1 WHERE seq = ?", (seq,))
            attempts = connection.execute("SELECT attempts FROM outbox WHERE seq = ?", (seq,)).fetchone()["attempts"]
            delay = (
                SEND_FIRST_RETRY_SECONDS
                if attempts < 2
                else min(SEND_RETRY_BASE_SECONDS * 2 ** min(attempts - 2, 10), SEND_RETRY_CAP_SECONDS)
            )
            next_attempt_at = time.time() + delay
            connection.execute("UPDATE outbox SET next_attempt_at = ? WHERE seq = ?", (next_attempt_at, seq))
        return next_attempt_at

    def record_activity(self, assignment_id: str, token: str, fingerprint: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT token_hash, status FROM assignments WHERE id = ?", (assignment_id,)
            ).fetchone()
            if row is None or not secrets.compare_digest(row["token_hash"], hash_token(token)):
                raise StaleAssignmentError(f"bad credentials for assignment {assignment_id}")
            if row["status"] != AssignmentStatus.RUNNING.value:
                raise StaleAssignmentError(f"assignment {assignment_id} has ended, activity refused")
            connection.execute(
                "INSERT INTO activities (assignment_id, fingerprint, at) VALUES (?,?,?)",
                (assignment_id, fingerprint, time.time()),
            )

    def recent_activity(self, assignment_id: str, limit: int) -> list[tuple[str, float]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT fingerprint, at FROM activities WHERE assignment_id = ? ORDER BY seq DESC LIMIT ?",
                (assignment_id, limit),
            ).fetchall()
        return [(row["fingerprint"], row["at"]) for row in reversed(rows)]

    def take_admitted(self, source: str, actor: str | None, message: str, max_age_seconds: float) -> list[Trigger]:
        now = time.time()
        spoken = message.strip()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM triggers WHERE admission = ? AND assessed_at IS NULL AND source = ? "
                "AND (? IS NULL OR actor = ?) AND received_at >= ? ORDER BY received_at",
                (Admission.ACT.value, source, actor, actor, now - max_age_seconds),
            ).fetchall()
            matching = [row for row in rows if row["text"].strip() and spoken.endswith(row["text"].strip())]
            connection.executemany(
                "UPDATE triggers SET assessed_at = ?, assessment = ? WHERE id = ?",
                [(now, OFFERED, row["id"]) for row in matching],
            )
        return [_trigger_from_row(row) for row in matching]

    def claim_offered_trigger(self, trigger_id: str, assessment: str, offered_within_seconds: float) -> bool:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE triggers SET assessed_at = ?, assessment = ? WHERE id = ? AND admission = ? "
                "AND assessment = ? AND assessed_at >= ?",
                (now, assessment, trigger_id, Admission.ACT.value, OFFERED, now - offered_within_seconds),
            )
            return cursor.rowcount == 1

    def last_watchdog_at(self, assignment_id: str) -> float | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(at) AS at FROM watchdog_events WHERE assignment_id = ?", (assignment_id,)
            ).fetchone()
        return float(row["at"]) if row["at"] is not None else None

    def add_watchdog_event(
        self, assignment_id: str, ticket_id: str, verdict: str, action: RepairAction, reason: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO watchdog_events (assignment_id, ticket_id, verdict, action, reason, at) "
                "VALUES (?,?,?,?,?,?)",
                (assignment_id, ticket_id, verdict, action.value, reason, time.time()),
            )

    def repair_count(self, ticket_id: str, action: RepairAction) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM watchdog_events e JOIN tickets t ON t.id = e.ticket_id "
                "WHERE e.ticket_id = ? AND e.action = ? AND e.at >= COALESCE(t.resumed_at, 0)",
                (ticket_id, action.value),
            ).fetchone()
        return int(row["n"])

    def watchdog_events(self, ticket_id: str) -> list[tuple[str, str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT verdict, action, reason FROM watchdog_events WHERE ticket_id = ? ORDER BY seq", (ticket_id,)
            ).fetchall()
        return [(row["verdict"], row["action"], row["reason"]) for row in rows]


def _insert_assignment(connection: sqlite3.Connection, ticket_id: str) -> tuple[Assignment, str]:
    token = secrets.token_urlsafe(24)
    assignment_id = f"as-{secrets.token_hex(4)}"
    now = time.time()
    attempt = (
        connection.execute("SELECT COUNT(*) AS n FROM assignments WHERE ticket_id = ?", (ticket_id,)).fetchone()["n"]
        + 1
    )
    connection.execute(
        "UPDATE assignments SET status = ?, ended_at = ? WHERE ticket_id = ? AND status = ?",
        (AssignmentStatus.ENDED.value, now, ticket_id, AssignmentStatus.RUNNING.value),
    )
    connection.execute(
        "INSERT INTO assignments (id, ticket_id, attempt, token_hash, status, pid, started_at) VALUES (?,?,?,?,?,?,?)",
        (assignment_id, ticket_id, attempt, hash_token(token), AssignmentStatus.RUNNING.value, None, now),
    )
    return Assignment(assignment_id, ticket_id, attempt, AssignmentStatus.RUNNING, None, now), token


def _set_scope_class(connection: sqlite3.Connection, ticket_id: str, scope_class: str) -> None:
    """Record a kastle-build-loop ticket's scope class. Once leo-gated it stays leo-gated: a later in-scope
    refuses the whole report, so a worker can never talk itself into the merge carve-out."""
    try:
        wanted = ScopeClass(scope_class)
    except ValueError as error:
        choices = " or ".join(item.value for item in ScopeClass)
        raise ValueError(f"scope_class must be {choices}, got {scope_class!r}") from error
    ticket = connection.execute("SELECT loop, scope_class FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if ticket is None or ticket["loop"] != Loop.KASTLE_BUILD.value:
        raise ValueError(f"scope_class is only accepted on {Loop.KASTLE_BUILD.value} tickets, report refused")
    if ticket["scope_class"] == ScopeClass.LEO_GATED.value and wanted is not ScopeClass.LEO_GATED:
        raise ValueError("this ticket is leo-gated and that never changes, report refused")
    connection.execute(
        "UPDATE tickets SET scope_class = ? WHERE id = ? AND (scope_class IS NULL OR scope_class != ?)",
        (wanted.value, ticket_id, ScopeClass.LEO_GATED.value),
    )


def _trigger_from_row(row: sqlite3.Row) -> Trigger:
    return Trigger(
        id=row["id"],
        source=row["source"],
        thread=row["thread"],
        actor=row["actor"],
        text=row["text"],
        channel=row["channel"],
        addressed=bool(row["addressed"]),
        is_bot=bool(row["is_bot"]),
    )


def _ticket_from_row(row: sqlite3.Row) -> Ticket:
    data = json.loads(row["mandate"])
    mandate = Mandate(
        policy_version=data["policy_version"],
        scope=data["scope"],
        strength=Strength(data["strength"]),
        actor=data["actor"],
        source=data["source"],
        trigger_id=data["trigger_id"],
    )
    return Ticket(
        row["id"],
        row["goal"],
        mandate,
        row["channel"],
        TicketStatus(row["status"]),
        row["loop"] or "",
        row["repo"] or "",
        row["scope_class"] or "",
    )


def _assignment_from_row(row: sqlite3.Row) -> Assignment:
    return Assignment(
        row["id"], row["ticket_id"], row["attempt"], AssignmentStatus(row["status"]), row["pid"], row["started_at"]
    )


def _report_from_row(row: sqlite3.Row) -> Report:
    return Report(
        row["assignment_id"], row["ticket_id"], ReportState(row["state"]), row["summary"], row["question"], row["at"]
    )
