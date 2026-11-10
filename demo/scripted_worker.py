from __future__ import annotations

import signal
import sys
import time
from pathlib import Path
from types import FrameType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from superagent.domain import ReportFields, ReportState  # noqa: E402
from superagent.reporting import identity_from_workdir, submit_report  # noqa: E402
from superagent.store import StaleAssignmentError  # noqa: E402


def say(text: str) -> None:
    print(text, flush=True)


def honest(goal: str) -> None:
    identity = identity_from_workdir(Path.cwd())
    for summary in ("reading the auth module", "reproduced the redirect loop", "fix written, tests pass"):
        submit_report(identity, ReportState.PROGRESS, summary)
        time.sleep(0.15)
    outcome = ReportFields(outcome="The pull request is open. Its tests pass.")
    submit_report(identity, ReportState.READY, f"pull request opened for: {goal[:60]}", fields=outcome)


def stuck() -> None:
    identity = identity_from_workdir(Path.cwd())
    terminated: list[int] = []

    def on_term(signum: int, frame: FrameType | None) -> None:
        terminated.append(signum)

    signal.signal(signal.SIGTERM, on_term)
    while not terminated:
        try:
            submit_report(identity, ReportState.PROGRESS, "still looking at the logs")
        except StaleAssignmentError:
            pass
        time.sleep(0.15)
    try:
        submit_report(identity, ReportState.READY, "all done, trust me")
    except StaleAssignmentError as error:
        say(f"late report refused: {error}")


def main() -> None:
    brief = Path(sys.argv[1]).read_text(encoding="utf-8")
    goal = brief.split("## Task\n", 1)[1].split("\n\n## Rules", 1)[0]
    attempt = identity_from_workdir(Path.cwd()).attempt
    if "impossible" in goal or ("flaky" in goal and attempt == 1):
        stuck()
        return
    honest(goal)


if __name__ == "__main__":
    main()
