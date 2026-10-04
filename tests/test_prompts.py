from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from orch.models import Priority, Risk, Task, TaskKind, TaskStatus
from orch.prompts import build_prompt, schema_for
from orch.router import AccessLevel


def make_task(kind: TaskKind, risk: Risk = Risk.NORMAL) -> Task:
    now = datetime.now(UTC)
    return Task(
        id="task-0001",
        repo_path=Path("/repo"),
        task="do the thing",
        risk=risk,
        priority=Priority.NORMAL,
        status=TaskStatus.RUNNING,
        created_at=now,
        updated_at=now,
        kind=kind,
        workspace_path=Path("/ws"),
    )


def test_prose_and_schema_instructions_are_never_both_present() -> None:
    """Asking for prose sections while enforcing a closed schema made grok emit a
    schema-shaped object every turn and never terminate, burning its whole budget."""
    structured = build_prompt(make_task(TaskKind.REVIEW), AccessLevel.READ_ONLY, structured=True)
    prose = build_prompt(make_task(TaskKind.IMPLEMENT), AccessLevel.WORKSPACE_WRITE)

    assert "exactly one JSON object" in structured
    assert "## Report at the end" not in structured

    assert "## Report at the end" in prose
    assert "exactly one JSON object" not in prose


def test_only_review_is_schema_constrained() -> None:
    assert schema_for(TaskKind.REVIEW) is not None
    for kind in TaskKind:
        if kind is not TaskKind.REVIEW:
            assert schema_for(kind) is None


def test_prompt_states_the_access_level_it_actually_runs_under() -> None:
    read_only = build_prompt(make_task(TaskKind.INVESTIGATE), AccessLevel.READ_ONLY)
    writing = build_prompt(make_task(TaskKind.IMPLEMENT), AccessLevel.WORKSPACE_WRITE)

    assert "This task is read-only." in read_only
    assert "inside the working directory only" in writing


def test_high_risk_is_planning_only() -> None:
    prompt = build_prompt(make_task(TaskKind.IMPLEMENT, Risk.HIGH), AccessLevel.READ_ONLY)

    assert "Implementing, editing, and deleting are all forbidden." in prompt


def test_every_kind_has_its_own_instructions() -> None:
    for kind in TaskKind:
        prompt = build_prompt(make_task(kind))
        assert "## How to approach this task" in prompt
        assert kind.value in prompt


def test_no_commit_or_push_is_always_stated() -> None:
    for kind in TaskKind:
        prompt = build_prompt(make_task(kind), structured=schema_for(kind) is not None)
        assert "Do not run git commit or git push" in prompt
