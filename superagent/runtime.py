from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .agents import redact
from .brief import BriefSettings, build_brief
from .config import DB_FILENAME, WORKDIRS_DIRNAME, Config, load_config
from .connectors import Connector, build_connector
from .domain import Ticket
from .ledger import Ledger
from .polling import SourceWarning, poll_sources
from .router import Assessor, Router, Sender, chat_safe, strip_links
from .store import Store, split_message
from .watchdog import CheckResult, DriftJudge, check_once
from .worker import SubprocessBackend

logger = logging.getLogger(__name__)

_SEND_TIMEOUT_SECONDS = 60
_TICK_LOCK_FILENAME = "tick.lock"
IGNORED_TRIGGER_RETENTION_SECONDS = 7 * 24 * 3600.0
POLL_BUDGET_SHARE = 0.5
SOURCE_TICKET = "sources"
_SEND_PLACEHOLDER = re.compile(r"\{(channel|text)\}")


@dataclass(frozen=True)
class Runtime:
    home: Path
    config: Config
    router: Router
    ledger: Ledger
    connectors: Mapping[str, Connector]

    @property
    def store(self) -> Store:
        return self.router.store


@dataclass(frozen=True)
class TickResult:
    new_tickets: tuple[Ticket, ...] = ()
    checks: tuple[CheckResult, ...] = ()
    delivered: tuple[tuple[str, str, str], ...] = ()
    errors: tuple[str, ...] = ()
    skipped: bool = False
    warnings: tuple[str, ...] = ()


def open_runtime(home: Path) -> Runtime:
    config = load_config(home)
    db_path = home / DB_FILENAME
    backend = SubprocessBackend(config.worker_argv, home / WORKDIRS_DIRNAME)
    router = Router(Store(db_path), config.rules, backend, config.max_running)
    backend.on_exit = router.start_queued
    connectors = {spec.name: build_connector(spec) for spec in config.connectors}
    return Runtime(home, config, router, Ledger(db_path), connectors)


def argv_sender(argv: tuple[str, ...]) -> Sender | None:
    if not argv:
        return None

    def send(channel: str, text: str) -> None:
        # The one place every outbound text passes, so it is neutralised here whatever wrote the outbox row: no
        # secret, nothing a chat app turns into a link or an attachment.
        values = {"channel": channel, "text": chat_safe(strip_links(redact(text)))}
        rendered = [_SEND_PLACEHOLDER.sub(lambda match: values[match.group(1)], part) for part in argv]
        subprocess.run(rendered, check=True, timeout=_SEND_TIMEOUT_SECONDS, capture_output=True)

    return send


def tick(runtime: Runtime, judge: DriftJudge | None = None, assessor: Assessor | None = None) -> TickResult:
    lock_fd = os.open(runtime.home / _TICK_LOCK_FILENAME, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stuck = _stuck_holder(_lock_record(lock_fd), runtime.config.tick_deadline_seconds)
            if stuck:
                _alert_at_once(runtime, stuck)
            return TickResult(skipped=True, errors=tuple(stuck))
        previous = _lock_record(lock_fd)
        started = time.time()
        _write_lock_record(lock_fd, {"pid": os.getpid(), "started": started})
        unfinished = _unfinished(previous)
        if unfinished:
            _alert_at_once(runtime, unfinished)
        result = _run_phases(runtime, judge, assessor)
        _write_lock_record(lock_fd, {"pid": os.getpid(), "started": started, "finished": time.time()})
        return replace(result, errors=(*unfinished, *result.errors))
    finally:
        os.close(lock_fd)


def _lock_record(lock_fd: int) -> dict[str, object]:
    try:
        record = json.loads(os.pread(lock_fd, 300, 0) or b"{}")
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _write_lock_record(lock_fd: int, record: dict[str, object]) -> None:
    os.ftruncate(lock_fd, 0)
    os.pwrite(lock_fd, json.dumps(record).encode(), 0)


def _age_minutes(record: dict[str, object]) -> float | None:
    started = record.get("started")
    if not isinstance(started, (int, float)):
        return None
    return (time.time() - float(started)) / 60


def _stuck_holder(holder: dict[str, object], deadline: float) -> list[str]:
    age = _age_minutes(holder)
    if "finished" in holder or age is None or age * 60 <= deadline:
        return []
    return [f"tick: another tick (pid {holder.get('pid')}) has held the lock for {age:.0f} minutes"]


def _unfinished(previous: dict[str, object]) -> list[str]:
    age = _age_minutes(previous)
    if "finished" in previous or age is None:
        return []
    return [
        f"tick: the tick started {age:.0f} minutes ago (pid {previous.get('pid')}) never finished, "
        "so it was probably stopped at its deadline"
    ]


def _alert_key(error: str) -> str:
    return re.sub(r"\d+", "#", error)[:200]


def _alert_at_once(runtime: Runtime, errors: list[str]) -> None:
    channel = runtime.config.alerts_channel
    try:
        queued = [runtime.store.raise_alert_now(_alert_key(error), chat_safe(error), channel) for error in errors]
        if any(queued):
            runtime.router.relay(argv_sender(runtime.config.send_argv), only_channel=channel)
    except Exception:
        logger.exception("could not raise an alert at once")


def _run_phases(runtime: Runtime, judge: DriftJudge | None, assessor: Assessor | None) -> TickResult:
    errors: list[str] = [f"config: {runtime.config.problem}"] if runtime.config.problem else []
    tickets: list[Ticket] = []
    checks: list[CheckResult] = []
    delivered: list[tuple[str, str, str]] = []
    warnings: list[SourceWarning] = []
    try:
        budget = runtime.config.tick_deadline_seconds * POLL_BUDGET_SHARE
        warnings = poll_sources(
            runtime.ledger,
            runtime.config.connectors,
            runtime.connectors,
            budget,
            lambda channel, text: runtime.store.queue_message(SOURCE_TICKET, channel, chat_safe(text)),
        )
    except Exception as error:
        logger.exception("polling failed")
        errors.append(f"poll: {error}")
    try:
        send_brief_when_due(runtime)
    except Exception as error:
        logger.exception("the brief failed")
        errors.append(f"brief: {error}")
    if assessor is not None:
        try:
            tickets = runtime.router.assess_candidates(assessor)
        except Exception as error:
            logger.exception("candidate assessment failed")
            errors.append(f"assess: {error}")
    try:
        checks = check_once(runtime.router, judge, runtime.config.watchdog)
    except Exception as error:
        logger.exception("watchdog check failed")
        errors.append(f"watchdog: {error}")
    try:
        delivered = runtime.router.relay(argv_sender(runtime.config.send_argv))
    except Exception as error:
        logger.exception("relay failed")
        errors.append(f"relay: {error}")
    if waiting := runtime.store.failed_send_count():
        errors.append(
            f"relay: {waiting} message(s) not delivered yet, retrying with backoff. `superagent outbox` lists them"
        )
    try:
        runtime.store.prune_ignored_triggers(IGNORED_TRIGGER_RETENTION_SECONDS)
    except Exception as error:
        logger.exception("pruning failed")
        errors.append(f"prune: {error}")
    try:
        delivered += _raise_alerts(runtime, errors, warnings)
    except Exception as error:
        logger.exception("alerting failed")
        errors.append(f"alerts: {error}")
    if not errors and runtime.config.heartbeat_argv:
        try:
            subprocess.run(
                runtime.config.heartbeat_argv, check=True, timeout=_SEND_TIMEOUT_SECONDS, capture_output=True
            )
        except (subprocess.SubprocessError, OSError) as error:
            errors.append(f"heartbeat: {error}")
    texts = tuple(warning.text for warning in warnings)
    return TickResult(tuple(tickets), tuple(checks), tuple(delivered), tuple(errors), warnings=texts)


def send_brief_when_due(runtime: Runtime, now: float | None = None) -> bool:
    if not runtime.config.brief_channel:
        return False
    moment = time.time() if now is None else now
    local = datetime.fromtimestamp(moment, ZoneInfo(runtime.config.brief_timezone))
    day = (local - timedelta(hours=runtime.config.brief_hour)).date().isoformat()
    if day in runtime.ledger.brief_days():
        return False
    return save_brief(runtime, day, moment)


def save_brief(runtime: Runtime, day: str, moment: float) -> bool:
    since = runtime.ledger.last_brief(moment) or moment - 86400
    settings = BriefSettings(runtime.config.stale_after_seconds, runtime.config.brief_timezone)
    live = {ticket.id for ticket in runtime.store.live_tickets()}
    brief = build_brief(runtime.ledger, runtime.config.connectors, since, moment, settings, live)
    first, *rest = split_message(brief.text)
    parts = [first, *(part.replace(") ", f") [{day}] ", 1) for part in rest)]
    return runtime.ledger.save_brief(day, moment, runtime.config.brief_channel, parts, brief.shown)


def _raise_alerts(runtime: Runtime, errors: list[str], warnings: list[SourceWarning]) -> list[tuple[str, str, str]]:
    active = {_alert_key(error): chat_safe(error) for error in errors}
    active.update({warning.key: chat_safe(warning.text) for warning in warnings})
    channel = runtime.config.alerts_channel
    immediate = frozenset(warning.key for warning in warnings)
    raised, resolved = runtime.store.sync_alerts(active, runtime.config.alert_after_seconds, channel, immediate)
    if not (raised or resolved):
        return []
    return runtime.router.relay(argv_sender(runtime.config.send_argv), only_channel=channel)
