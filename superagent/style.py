"""Plain output, about 80% of ASD-STE100 (tuning plan A.2). check_field() judges one field a worker or the router
wrote; check_message() judges a whole rendered message, where the template's own structure is allowed."""

from __future__ import annotations

import re
from typing import NamedTuple

MAX_SENTENCE_WORDS = 20
MAX_COMMAS = 2
MAX_PATH_CHARS = 40
MAX_REPORT_LINES = 6
MAX_STATUS_LINES = 10
PLAIN_WORDS = {
    "utilize": "use",
    "utilise": "use",
    "leverage": "use",
    "ensure": "make sure",
    "in order to": "to",
    "prior to": "before",
    "subsequent to": "after",
    "commence": "start",
    "initiate": "start",
    "facilitate": "help",
    "demonstrate": "show",
    "approximately": "about",
}

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# 7 to 40 hex characters holding a digit and a letter, so 364, 2026 and words such as "decade" stay. A ticket id
# (tk-ee69ce72) follows a hyphen and is a name, not a commit.
_SHA = re.compile(r"(?<![\w-])(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![\w-])")
_RUN_ID = re.compile(r"(?<![\w.])\d{9,}(?![\w.])")
_WORDS = [(re.compile(rf"(?<![\w-]){re.escape(word)}(?![\w-])", re.IGNORECASE), word) for word in PLAIN_WORDS]
_STATES = ("✅ Done", "⛔ Needs you", "❌ Failed", "🚨 Stuck", "▶️ Started", "⏹️ Cancelled", "Details")
_HEADER = re.compile(rf"^(?:{'|'.join(map(re.escape, _STATES))}) · (?P<title>.+) · tk-[0-9a-f]{{8}}$")
_CHECKS = "Checks: "
_BULLET = "• "

HOW_TO_WRITE = """How to write: plain English, about 80% of ASD-STE100.
- Verdict first: the result, the decision or the blocker goes in the first sentence.
- One idea in each sentence. Max 20 words in each sentence. Max 6 lines in each message.
- Active voice. Use the same word for the same thing every time.
- No parentheses. Max 2 commas in each sentence. No "·" or ";" chains.
- No commit SHAs, no run ids, no file paths longer than 40 characters. Names stay: repos, PR numbers, job names, \
file names.
- Plain words: "use", not "utilize" or "leverage". "Make sure", not "ensure". "To", not "in order to". "Before", \
not "prior to".
Examples from real messages, bad then good:
1. Bad: "Done [In Rhyzome-Limited/rhyzome-superagent-guards, fix the failing ... · tk-ee69ce72]: PR #4 · cause: \
relay-guard tests hardcode /bin/bash 3.2 semantics, ubuntu-latest has bash 5 (6 fails). Fix: that job now runs-on \
macos-latest (commit 2c512b5, ci.yml only, 2+/1-, no test touched); ..."
   Good: "The relay-guard tests now run on macOS. All 3 CI jobs pass. PR #4 is ready to merge."
2. Bad: the router's own reply after a dispatch, "**Worker started** - **Ticket:** tk-15feb607 - **Repo:** \
kastle-extension - **Loop:** kastle-build-loop - **State:** `starting` **Work plan I gave the worker** - Read the \
Notion issue. ..."
   Good: reply with exactly [SILENT]. Code already sent "▶️ Started · Covenant fee fix · tk-15feb607".
3. Bad: "Since the repo has no workflow at all and `gh pr checks 4` already passes, should I add a minimal CI \
workflow to superagent-sandbox so PR #4 has a real passing check, or treat the gate as already satisfied and leave \
the PR untouched?"
   Good: "Should the worker add a small CI workflow to PR #4? The gate already passes without one.\""""


class Problem(NamedTuple):
    rule: str
    message: str


def check_field(field: str, text: str, max_words: int | None = None) -> list[Problem]:
    """The A.2 rules for one field, plus its own word limit."""
    problems = _rules(field, text)
    words = len(text.split())
    if max_words is not None and words > max_words:
        problems.append(Problem("field-words", f"{field} has {words} words; the limit is {max_words}. Rewrite it."))
    return problems


def check_message(text: str, max_lines: int = MAX_REPORT_LINES) -> list[Problem]:
    """The A.2 rules for a whole message. Line 1's "·" separators, the "• " bullets, the checks line's "·" and the
    icons are the template's own text, so only what sits between them is checked."""
    lines = text.split("\n")
    problems = []
    if len(lines) > max_lines:
        problems.append(Problem("message-lines", f"the message has {len(lines)} lines; the limit is {max_lines}"))
    for number, line in enumerate(lines, 1):
        for part in _written_parts(line):
            problems += _rules(f"line {number}", part)
    return problems


def _written_parts(line: str) -> list[str]:
    if header := _HEADER.match(line):
        return [header["title"]]
    if line.startswith(_CHECKS):
        return [item.removesuffix(" ✓").removesuffix(" ✗") for item in line[len(_CHECKS) :].split(" · ")]
    return [line.removeprefix(_BULLET)]


def _rules(where: str, text: str) -> list[Problem]:
    problems = []
    for sentence in _SENTENCE_END.split(text.strip()):
        if (words := len(sentence.split())) > MAX_SENTENCE_WORDS:
            problems.append(
                Problem("sentence-words", f"{where} has a sentence of {words} words; the limit is {MAX_SENTENCE_WORDS}")
            )
        if (commas := sentence.count(",")) > MAX_COMMAS:
            problems.append(
                Problem("commas", f"{where} has a sentence with {commas} commas; the limit is {MAX_COMMAS}")
            )
    if "(" in text or ")" in text:
        problems.append(Problem("parentheses", f"{where} has parentheses; write a second sentence instead"))
    if "·" in text or ";" in text:
        problems.append(Problem("chain", f'{where} chains parts with "·" or ";"; write full sentences'))
    if sha := _SHA.search(text):
        problems.append(Problem("sha", f"{where} has a commit SHA ({sha[0]}); put it in details"))
    if run := _RUN_ID.search(text):
        problems.append(Problem("run-id", f"{where} has a run id ({run[0]}); put it in details"))
    if any("/" in token and len(token) > MAX_PATH_CHARS for token in text.split()):
        problems.append(Problem("long-path", f"{where} has a path longer than {MAX_PATH_CHARS} characters"))
    for pattern, word in _WORDS:
        if pattern.search(text):
            problems.append(Problem("plain-words", f"{where} uses {word!r}; write {PLAIN_WORDS[word]!r}"))
    return problems
