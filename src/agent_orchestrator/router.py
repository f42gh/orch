from __future__ import annotations

from agent_orchestrator.models import Risk


def resolve_worker_mode(risk: Risk) -> str:
    if risk == Risk.READ_ONLY:
        return "read-only worker"
    if risk == Risk.HIGH:
        return "planning-only worker"
    return "coding worker"


def resolve_allowed_tools(risk: Risk) -> list[str]:
    if risk == Risk.READ_ONLY:
        return ["Read", "Grep", "Glob", "Bash"]
    if risk == Risk.HIGH:
        return ["Read", "Grep", "Glob", "Bash"]
    return ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]
