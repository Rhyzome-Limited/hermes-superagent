from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_demo_runs_the_whole_vertical_slice() -> None:
    done = subprocess.run(
        [sys.executable, str(ROOT / "demo" / "run_demo.py")], capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr
    output = done.stdout
    assert "pull request opened for: fix the login redirect loop" in output
    assert "✅ Done · fix the login redirect loop · tk-" in output
    assert "The pull request is open. Its tests pass." in output
    assert "actor is not on the allowed list" in output
    assert "was not admitted by the gate" in output
    assert "tickets opened for this event: 0" in output
    assert "stuck -> nudge" in output
    assert "stuck -> restart" in output
    assert "late report refused" in output
    assert "stuck -> escalate" in output
    assert "The worker sent the same report 3 times." in output
    assert "🚨 Stuck · solve the impossible migration · tk-" in output
    assert "still open: 0" in output
