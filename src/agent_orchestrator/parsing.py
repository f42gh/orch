"""Turning caller-supplied strings into the enums and requests the core expects.

Both entry points take the same values from an untrusted caller — `orch` from
argv and a tasks file, `mcp_server` from a JSON tool call — and both used to validate
them with their own copy of these rules. The copies had already drifted apart in their
wording, which meant the same bad input was reported two different ways depending on
which door it came through.

Everything here raises `WorkflowError`. The MCP server converts that to `DispatchError`
at its boundary via `_workflow_call`, so its outward error type is unchanged.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TextIO

from agent_orchestrator.models import (
    Engine,
    FallbackMode,
    Priority,
    Risk,
    TaskKind,
    WorkflowRouteOverride,
    WorkflowTaskRequest,
)
from agent_orchestrator.workflows import WorkflowError


#: `agy` is what the Antigravity CLI is called on disk; the enum keeps the long name.
ENGINE_ALIASES = {"agy": Engine.ANTIGRAVITY.value}

#: The only keys a task object may carry. Anything else is a caller mistake worth
#: reporting rather than ignoring — `engine` in particular, because a workflow route
#: chooses the engine and silently dropping the key would look like it had been honoured.
TASK_REQUEST_FIELDS = frozenset(
    {"task", "kind", "risk", "priority", "parent_id", "base_ref"}
)


def parse_enum[T: Enum](enum: type[T], value: str | None, default: T | None = None) -> T | None:
    if value is None:
        return default
    try:
        return enum(value)
    except ValueError:
        allowed = ", ".join(member.value for member in enum)
        raise WorkflowError(f"{value!r} is not one of: {allowed}") from None


def parse_engine(value: str | None) -> Engine | None:
    if value is None:
        return None
    normalized = ENGINE_ALIASES.get(value.strip(), value.strip())
    try:
        return Engine(normalized)
    except ValueError:
        allowed = ", ".join([*(engine.value for engine in Engine), "agy"])
        raise WorkflowError(f"engine {value!r} is not one of: {allowed}") from None


def require_engine(value: str) -> Engine:
    """`parse_engine` for callers that already know they have a value."""
    engine = parse_engine(value)
    assert engine is not None
    return engine


def parse_kind(value: str) -> TaskKind:
    try:
        return TaskKind(value.strip())
    except ValueError:
        allowed = ", ".join(kind.value for kind in TaskKind)
        raise WorkflowError(f"task kind {value!r} is not one of: {allowed}") from None


def parse_iso_datetime(value: str | None, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise WorkflowError(f"invalid {field} datetime {value!r}; expected ISO 8601") from None


def parse_assignment(value: str, *, shape: str) -> tuple[str, str]:
    if "=" not in value:
        raise WorkflowError(f"expected {shape}, got {value!r}")
    left, right = value.split("=", 1)
    if not left.strip() or not right.strip():
        raise WorkflowError(f"expected {shape}, got {value!r}")
    return left.strip(), right.strip()


def route_overrides(
    primaries: Mapping[str, str] | None,
    fallbacks: Mapping[str, Sequence[str]] | None,
) -> dict[TaskKind, WorkflowRouteOverride]:
    """Build the route snapshot a workflow is created with.

    A supplied fallback list is strict and exhaustive, so it may only accompany an
    explicit primary: inferring one would silently widen a list the caller wrote to
    narrow the choice.
    """
    parsed_primaries: dict[TaskKind, Engine] = {}
    for raw_kind, raw_engine in (primaries or {}).items():
        parsed_primaries[parse_kind(raw_kind)] = require_engine(raw_engine)

    parsed_fallbacks: dict[TaskKind, tuple[Engine, ...]] = {}
    for raw_kind, raw_chain in (fallbacks or {}).items():
        kind = parse_kind(raw_kind)
        if kind not in parsed_primaries:
            raise WorkflowError(
                f"fallbacks for {kind.value} require a matching primary route"
            )
        if isinstance(raw_chain, str) or not raw_chain:
            raise WorkflowError(f"fallbacks for {kind.value} must be a non-empty engine list")
        chain: list[Engine] = []
        for raw_engine in raw_chain:
            if not isinstance(raw_engine, str) or not raw_engine.strip():
                raise WorkflowError(
                    f"fallbacks for {kind.value} must contain non-empty engine names"
                )
            chain.append(require_engine(raw_engine))
        parsed_fallbacks[kind] = tuple(chain)

    overrides: dict[TaskKind, WorkflowRouteOverride] = {}
    for kind, primary in parsed_primaries.items():
        chain_for_kind = parsed_fallbacks.get(kind)
        try:
            overrides[kind] = (
                WorkflowRouteOverride(primary)
                if chain_for_kind is None
                else WorkflowRouteOverride(primary, FallbackMode.MANUAL, chain_for_kind)
            )
        except ValueError as exc:
            raise WorkflowError(f"route {kind.value}: {exc}") from None
    return overrides


def route_overrides_from_flags(
    route_flags: Sequence[str], fallback_flags: Sequence[str]
) -> dict[TaskKind, WorkflowRouteOverride]:
    """`--route KIND=PRIMARY` and `--fallback KIND=E1,E2` folded into `route_overrides`.

    A repeated flag is rejected rather than last-wins: the caller asked for two
    different routes for one kind and only one of them can be what they meant.
    """
    primaries: dict[str, str] = {}
    for raw in route_flags:
        kind_raw, engine_raw = parse_assignment(raw, shape="KIND=PRIMARY")
        kind = parse_kind(kind_raw)
        if kind.value in primaries:
            raise WorkflowError(f"duplicate --route for {kind.value}")
        primaries[kind.value] = engine_raw

    fallbacks: dict[str, Sequence[str]] = {}
    for raw in fallback_flags:
        kind_raw, engines_raw = parse_assignment(raw, shape="KIND=E1,E2")
        kind = parse_kind(kind_raw)
        if kind.value in fallbacks:
            raise WorkflowError(f"duplicate --fallback for {kind.value}")
        fallbacks[kind.value] = [part.strip() for part in engines_raw.split(",")]

    return route_overrides(primaries, fallbacks)


def task_request(value: object, *, where: str) -> WorkflowTaskRequest:
    if not isinstance(value, Mapping):
        raise WorkflowError(f"{where} must be a JSON object")
    unknown = sorted(str(key) for key in value if key not in TASK_REQUEST_FIELDS)
    if unknown:
        raise WorkflowError(f"{where} has unknown fields: {', '.join(unknown)}")
    task = value.get("task")
    if not isinstance(task, str) or not task.strip():
        raise WorkflowError(f"{where}.task must be a non-empty string")
    try:
        kind = parse_enum(TaskKind, value.get("kind"), TaskKind.IMPLEMENT)
        risk = parse_enum(Risk, value.get("risk"), Risk.NORMAL)
        priority = parse_enum(Priority, value.get("priority"), Priority.NORMAL)
    except WorkflowError as exc:
        raise WorkflowError(f"{where}: {exc}") from None
    parent_id = value.get("parent_id")
    base_ref = value.get("base_ref")
    if parent_id is not None and not isinstance(parent_id, str):
        raise WorkflowError(f"{where}.parent_id must be a string or null")
    if base_ref is not None and not isinstance(base_ref, str):
        raise WorkflowError(f"{where}.base_ref must be a string or null")
    assert isinstance(kind, TaskKind)
    assert isinstance(risk, Risk)
    assert isinstance(priority, Priority)
    return WorkflowTaskRequest(
        task=task.strip(),
        kind=kind,
        risk=risk,
        priority=priority,
        parent_id=parent_id,
        base_ref=base_ref,
    )


def load_tasks_file(path: str, *, stdin: TextIO | None = None) -> tuple[WorkflowTaskRequest, ...]:
    in_stream = stdin if stdin is not None else sys.stdin
    if path == "-":
        raw = in_stream.read()
    else:
        task_path = Path(path).expanduser()
        if not task_path.exists():
            raise WorkflowError(f"tasks file does not exist: {task_path}")
        raw = task_path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"tasks file is not valid JSON: {exc}") from None
    if not isinstance(payload, list):
        raise WorkflowError("tasks file must contain a JSON array")
    if not payload:
        raise WorkflowError("a batch must contain at least one task")
    return tuple(
        task_request(item, where=f"tasks[{index}]") for index, item in enumerate(payload)
    )
