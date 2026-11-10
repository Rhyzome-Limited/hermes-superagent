from __future__ import annotations

from collections.abc import Mapping

from .worker import ROLE_ROUTER, ROLE_WATCHDOG, ROLE_WORKER

TOOL_DISPATCH = "superagent_dispatch"
TOOL_STATUS = "superagent_status"
TOOL_REPORT = "superagent_report"
TOOL_TICK = "superagent_tick"
TOOL_FORWARD = "superagent_forward"
TOOL_CANCEL = "superagent_cancel"
TOOL_DIGEST = "superagent_digest"

TOOL_BRIDGE_CALL = "tool_call"
# Hermes defers plugin tools behind these bridges by default.
TOOL_SEARCH_BRIDGES = frozenset({"tool_search", "tool_describe", TOOL_BRIDGE_CALL})
# No memory or session_search: injected text must not persist into, or be fished out of, other sessions.
# wiki_find and wiki_read are the read-only wiki mirror tools (toolset "wiki").
ROUTER_ALLOWED_TOOLS = TOOL_SEARCH_BRIDGES | {
    TOOL_DISPATCH,
    TOOL_FORWARD,
    TOOL_CANCEL,
    TOOL_STATUS,
    "skills_list",
    "skill_view",
    "clarify",
    "wiki_find",
    "wiki_read",
}
WATCHDOG_ALLOWED_TOOLS = TOOL_SEARCH_BRIDGES | {TOOL_TICK, TOOL_DIGEST, TOOL_STATUS, "skills_list", "skill_view"}
WORKER_BLOCKED_TOOLS = frozenset(
    {TOOL_DISPATCH, TOOL_FORWARD, TOOL_CANCEL, TOOL_TICK, TOOL_DIGEST, TOOL_STATUS, "send_message", "cronjob_manage"}
)
WORKER_HOST_TOOLS = frozenset(
    {"terminal", "process_manage", "execute_code", "read_file", "write_file", "patch", "search_files", "computer_use"}
)


def _bridged_tool_names(args: object) -> list[str] | None:
    calls = args.get("calls") if isinstance(args, Mapping) else None
    if not isinstance(calls, list) or not calls:
        return None
    names: list[str] = []
    for call in calls:
        name = call.get("name") if isinstance(call, Mapping) else None
        if not isinstance(name, str) or not name:
            return None
        names.append(name)
    return names


def blocked_reason(role: str, tool_name: str, *, worker_sandboxed: bool = True, args: object = None) -> str | None:
    if tool_name == TOOL_BRIDGE_CALL and role in (ROLE_ROUTER, ROLE_WATCHDOG, ROLE_WORKER):
        names = _bridged_tool_names(args)
        if not names:
            return f"{TOOL_BRIDGE_CALL} needs a list of named calls."
        for name in names:
            if name in TOOL_SEARCH_BRIDGES:
                return f"{TOOL_BRIDGE_CALL} cannot call {name}."
            if reason := blocked_reason(role, name, worker_sandboxed=worker_sandboxed):
                return reason
        return None
    if role == ROLE_ROUTER and tool_name not in ROUTER_ALLOWED_TOOLS:
        return f"The router dispatches and never executes. {tool_name} is not available here, use superagent_dispatch."
    if role == ROLE_WATCHDOG and tool_name not in WATCHDOG_ALLOWED_TOOLS:
        return f"The watchdog only observes and repairs through superagent_tick. {tool_name} is not available here."
    if role == ROLE_WORKER and tool_name in WORKER_BLOCKED_TOOLS:
        return f"Workers report only to the router through superagent_report. {tool_name} is not available here."
    if role == ROLE_WORKER and tool_name in WORKER_HOST_TOOLS and not worker_sandboxed:
        return (
            f"{tool_name} would run as the same OS user as the router, and could rewrite its config and database. "
            "Give the worker profile a sandboxed terminal backend such as docker, or set allow_same_user in "
            "[worker] to accept that risk."
        )
    return None
