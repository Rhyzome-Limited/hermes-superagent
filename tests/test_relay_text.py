from __future__ import annotations

import re
import time

import pytest

from superagent import router
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


DOTS = ".\u3002\uff0e\uff61"
# IANA's IDN example host under the real ccTLD .bharat (xn--p1b6ci4b4b3a.xn--h2brj9c). Its vowel signs are
# combining marks, which \w does not match.
DEVANAGARI_HOST = "\u0909\u0926\u093e\u0939\u0930\u0923.\u092d\u093e\u0930\u0924"
_HOST_LETTER = re.compile(r"[\w\xad-]")


def in_host(char: str) -> bool:
    """Telegram's rule for a character of a host name, written apart from the code under test: below U+00C0 a
    letter, digit, "_" or "-" (or the soft hyphen IDNA drops), from U+00C0 up anything but a space or a dot."""
    if char in DOTS:
        return False
    return bool(_HOST_LETTER.fullmatch(char)) or (ord(char) >= 0xC0 and not char.isspace())


def linkable(text: str) -> bool:
    """A dot with a host character on each side is what an auto-linker needs to see a host."""
    return any(text[at] in DOTS and in_host(text[at - 1]) and in_host(text[at + 1]) for at in range(1, len(text) - 1))


def test_the_host_class_follows_telegrams_rule_for_every_code_point() -> None:
    expected = [in_host(chr(code)) for code in range(0x110000)]
    for flags in (0, re.IGNORECASE):
        host_char = re.compile(router._HOST_CHAR, flags)
        wrong = [hex(code) for code in range(0x110000) if bool(host_char.fullmatch(chr(code))) != expected[code]]
        assert wrong == []


@pytest.mark.parametrize(
    "text",
    [
        "see attacker.example?d=leaked-value",
        "see attacker.com#frag",
        "see evil.co.uk?q=1",
        "see 1.2.3.4/x",
        "see attacker.com:8080/x",
        "see x_attacker.com?d=1",
        "see attacker\uff0ecom/x",
        "see \u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444/x",
        f"see {DEVANAGARI_HOST}/x?d=LEAK",
        "see \u0909\u0926\u093e\u0939\u0930\u0923.\u0915\u0949\u092e/x?d=LEAK",
        "see \u0989\u09a6\u09be\u09b9\u09b0\u09a3.\u09ad\u09be\u09b0\u09a4/x?d=LEAK",
        "see \u0909\u0926\u093e\u0939\u0930\u0923\u093e.com/x?d=LEAK",
        "see attacker\u0301.com/x?d=LEAK",
        "see attacker\u200d.com/x?d=LEAK",
        "see attacker.co\u0301m/x?d=LEAK",
        "see i\u2764.ws/x?d=LEAK",
        "see attack\u00ader.com/x?d=LEAK",
    ],
)
def test_a_bare_host_with_a_path_query_or_fragment_is_removed(text: str) -> None:
    line = rendered(text)
    assert line.endswith(": see [link removed]")
    assert not linkable(line)


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("see attacker.com", "see attacker[.]com"),
        ("see secret-data.attacker.com.", "see secret-data[.]attacker[.]com."),
        ("mail me@attacker.com", "mail me@attacker[.]com"),
        ("dial 10.0.0.1", "dial 10[.]0[.]0[.]1"),
        ("see attacker.com:8080", "see attacker[.]com:8080"),
        ("see attacker\u3002com", "see attacker[.]com"),
        ("see xn--e1afmkfd.xn--p1ai", "see xn--e1afmkfd[.]xn--p1ai"),
        ("fixed router.py:356 and README.md", "fixed router[.]py:356 and README[.]md"),
        (f"mail {DEVANAGARI_HOST}", "mail " + DEVANAGARI_HOST.replace(".", "[.]")),
        ("see attackeŕ.com", "see attackeŕ[.]com"),
        ("see attacker‌.com", "see attacker‌[.]com"),
        ("see attacker.cóm", "see attacker[.]cóm"),
    ],
)
def test_any_other_host_keeps_its_name_but_cannot_become_a_link(text: str, kept: str) -> None:
    line = rendered(text)
    assert line.endswith(kept)
    assert not linkable(line)


def test_versions_and_abbreviations_are_not_hosts() -> None:
    text = "bumped to v0.22.7 and Python 3.12, e.g. U.S.A. Ph.D wait...then"
    assert strip_links(text) == text


def test_an_escalation_with_a_bare_host_is_cleaned_too() -> None:
    line = _render_escalation("the worker says: open attacker.example?d=leaked and data.attacker.example", "tk-1", GOAL)
    assert "leaked" not in line
    assert line.endswith("open [link removed] and data[.]attacker[.]example")


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


@pytest.mark.parametrize(
    "unit",
    ["ab.", "1.", "a+b-c.", "www.", "[a](", "![a](", "[", "<img", "[a]", "](", "हरण.", "á.", "‍."],
)
def test_hostile_text_is_cleaned_in_linear_time(unit: str) -> None:
    # A worker writes the report, and the patterns run before any length cap: 100k characters must stay cheap.
    text = unit * (100_000 // len(unit))
    started = time.perf_counter()
    rendered(text)
    assert time.perf_counter() - started < 1.0
