from __future__ import annotations

import pytest

from superagent.domain import Report, ReportState
from superagent.router import _render_escalation, _render_report, chat_safe, strip_links

GOAL = "fix the krc721 loading spinner"


def rendered(text: str, state: ReportState = ReportState.READY, question: str = "") -> str:
    line = _render_report(Report("as-1", "tk-1", state, text, question, 0.0), GOAL)
    assert line is not None
    return line


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "a1B2" * 9,
        "github_pat_" + "x" * 30,
        "sk-" + "Q" * 24,
        "xoxb-1234567890-abcdefghij",
        "AKIA" + "A" * 16,
        "-----BEGIN OPENSSH PRIVATE KEY-----",
    ],
)
def test_a_report_never_carries_a_secret_to_chat(secret: str) -> None:
    line = rendered(f"done, used {secret} to push")
    assert secret not in line
    assert "[redacted]" in line


def test_key_value_secrets_in_a_question_are_redacted() -> None:
    line = rendered(
        "summary", ReportState.BLOCKED, question="Should I reuse api_key=hunter2hunter2 or Bearer abcdefgh123?"
    )
    assert "hunter2" not in line
    assert "abcdefgh123" not in line
    assert line.startswith("Blocked [")


@pytest.mark.parametrize(
    "text",
    [
        "done ![chart](https://evil.example/pixel.png?u=leo)",
        'done <img src="https://evil.example/pixel.png">',
        "done ![](http://10.0.0.1/x.png)",
    ],
)
def test_image_tags_are_removed(text: str) -> None:
    line = rendered(text)
    assert "evil" not in line and "10.0.0.1" not in line
    assert "![" not in line and "<img" not in line.lower()


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("see [the fix](https://evil.example/login) here", "see the fix here"),
        ("open https://evil.example/login?next=/ now", "open [link removed] now"),
        ("open www.evil.example/login now", "open [link removed] now"),
        ("open evil.example/login now", "open [link removed] now"),
        ("download ftp://files.example/payload", "download [link removed]"),
    ],
)
def test_links_are_removed(text: str, kept: str) -> None:
    line = rendered(text)
    assert "evil" not in line and "files.example" not in line
    assert line.endswith(kept)


def test_a_github_pull_request_keeps_its_reference_without_a_link() -> None:
    line = rendered("PR opened: https://github.com/Rhyzome-Limited/kastle-mobile/pull/412/files and merged")
    assert line.endswith("PR opened: Rhyzome-Limited/kastle-mobile#412 and merged")
    assert "https" not in line
    assert strip_links("see github.com/acme/app/issues/7.") == "see acme/app#7."


def test_escalations_are_cleaned_the_same_way() -> None:
    line = _render_escalation("worker says: paste ghp_" + "Z" * 36 + " into https://evil.example/x", "tk-1", GOAL)
    assert "ghp_" not in line and "evil" not in line
    assert line.startswith("Needs you [")


def test_progress_still_stays_quiet() -> None:
    assert _render_report(Report("as-1", "tk-1", ReportState.PROGRESS, "https://x.example", "", 0.0), GOAL) is None


def test_chat_safe_breaks_any_image_tag_and_stays_idempotent() -> None:
    once = chat_safe("look ![x](https://e.example/p.png) MEDIA:/etc/hosts [[as_document]] @everyone")
    assert "![" not in once
    assert "MEDIA:" not in once.upper()
    assert "[[" not in once
    assert chat_safe(once) == once
