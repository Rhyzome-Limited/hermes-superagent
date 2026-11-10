from __future__ import annotations

import pytest

from superagent import style

# The real "Done" message for tk-ee69ce72 (outbox seq 26 in ~/superagent/p2/superagent.db, 2026-11-10).
TK_EE69CE72 = (
    "Done [In Rhyzome-Limited/rhyzome-superagent-guards, fix the failing relay-guard-tests ... · tk-ee69ce72]: "
    "PR #4 Rhyzome-Limited/rhyzome-superagent-guards#4 · cause: relay-guard tests hardcode /bin/bash 3.2 semantics, "
    "ubuntu-latest has bash 5 (6 fails). Fix: that job now runs-on macos-latest (commit 2c512b5, ci[.]yml only, "
    "2+/1-, no test touched); wiki-mirror and shell-syntax stay ubuntu-latest + py3.11. Run 42756344802 on "
    "macos-26-arm64: relay-guard 364 passed, wiki-mirror pass, shell syntax pass; gate `gh pr checks 4` exit 0, PR "
    "left non-draft (was already ready), MERGEABLE."
)


def rules(problems: list[style.Problem]) -> set[str]:
    return {problem.rule for problem in problems}


@pytest.mark.parametrize(
    ("rule", "bad", "good"),
    [
        (
            "sentence-words",
            "The worker moved the relay guard job to macOS because the tests need bash three point two and Linux "
            "runs bash five.",
            "The relay guard job now runs on macOS. The tests need bash 3.2.",
        ),
        ("parentheses", "All 3 jobs pass (two on Linux).", "All 3 jobs pass. Two run on Linux."),
        ("commas", "It changed the CI file, the README, the plan, and the test list.", "It changed the CI file."),
        ("chain", "PR #4 · CI green · ready", "PR #4 is ready. CI is green."),
        ("chain", "The job runs on macOS; the rest stay on Linux.", "The job runs on macOS. The rest stay on Linux."),
        ("sha", "The fix is in commit 2c512b5.", "The fix is in PR #4."),
        ("run-id", "Run 42756344802 passed.", "The CI run passed."),
        (
            "long-path",
            "It edited src/rhyzome-superagent-guards/ops/profiles/router/config.yaml only.",
            "It edited the router config.yaml only.",
        ),
    ],
)
def test_each_rule_fails_a_bad_field_and_passes_a_good_one(rule: str, bad: str, good: str) -> None:
    assert rule in rules(style.check_field("outcome", bad))
    assert style.check_field("outcome", good) == []


@pytest.mark.parametrize(("word", "plain"), sorted(style.PLAIN_WORDS.items()))
def test_each_word_on_the_list_names_its_plain_replacement(word: str, plain: str) -> None:
    [problem] = style.check_field("outcome", f"We {word} the new gate.")
    assert problem.rule == "plain-words"
    assert repr(plain) in problem.message
    assert style.check_field("outcome", f"We {plain} the new gate.") == []


def test_a_word_list_entry_matches_whole_words_only() -> None:
    assert style.check_field("outcome", "The ensurer read the prior art.") == []
    assert "plain-words" in rules(style.check_field("outcome", "Ensure the gate runs."))


def test_names_stay() -> None:
    text = "PR #4 in rhyzome-superagent-guards changed ci.yml. The relay-guard job and tk-ee69ce72 pass."
    assert style.check_field("outcome", text) == []


def test_numbers_and_hex_words_are_not_ids() -> None:
    assert style.check_field("outcome", "364 tests passed in 2026. The decade-old cafe code stays.") == []
    assert style.check_field("outcome", "Version v0.22.7 ships to 1234567 users.") == []


def test_field_word_limit_names_the_field_and_the_limit() -> None:
    outcome = " ".join(["word"] * 17) + ". " + " ".join(["more"] * 17) + "."
    [problem] = [p for p in style.check_field("outcome", outcome, max_words=20) if p.rule == "field-words"]
    assert problem.message == "outcome has 34 words; the limit is 20. Rewrite it."
    assert [
        p for p in style.check_field("outcome", "All 3 CI jobs pass.", max_words=20) if p.rule == "field-words"
    ] == []


def test_message_line_limit_is_6_for_a_report_and_10_for_a_status_reply() -> None:
    seven = "\n".join(f"Line {n} is short." for n in range(7))
    assert "message-lines" in rules(style.check_message(seven))
    assert "message-lines" not in rules(style.check_message(seven, max_lines=style.MAX_STATUS_LINES))
    eleven = "\n".join(f"Line {n} is short." for n in range(11))
    assert "message-lines" in rules(style.check_message(eleven, max_lines=style.MAX_STATUS_LINES))
    assert style.check_message("\n".join(f"Line {n} is short." for n in range(6))) == []


def test_template_structure_is_code_written_and_allowed() -> None:
    message = "✅ Done · Guards CI fix · tk-ee69ce72\n• Changed the CI file.\nChecks: relay-guard ✓ · lint ✗"
    assert style.check_message(message) == []


def test_a_chain_inside_a_message_line_still_fails() -> None:
    message = "✅ Done · Guards CI fix · tk-ee69ce72\nPR #4 · CI green · ready"
    assert "chain" in rules(style.check_message(message))


def test_a_bad_title_in_line_one_still_fails() -> None:
    assert "sha" in rules(style.check_message("✅ Done · Fix 2c512b5 · tk-ee69ce72\nAll 3 CI jobs pass."))


def test_the_real_tk_ee69ce72_message_fails_at_least_4_rules() -> None:
    failed = rules(style.check_message(TK_EE69CE72))
    assert len(failed) >= 4, failed
    assert {"sentence-words", "parentheses", "commas", "chain", "sha", "run-id"} <= failed


def test_the_real_tk_ee69ce72_report_fails_as_an_outcome() -> None:
    summary = TK_EE69CE72.split("]: ", 1)[1]
    assert len(rules(style.check_field("outcome", summary, max_words=20))) >= 4
