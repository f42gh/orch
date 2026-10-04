"""Decide which engine runs a task, and under what access level.

The orchestrator (Claude) picks a `TaskKind`; this module deterministically picks the
engine. Keeping the choice here rather than in the prompt means it is testable, and it
means an engine that is not installed degrades to a working one instead of failing.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Mapping

from orch.models import (
    WRITING_KINDS,
    Engine,
    FallbackMode,
    Risk,
    TaskKind,
    WorkflowRoute,
    WorkflowRouteOverride,
)


class AccessLevel(StrEnum):
    """Engine-agnostic access level. Each adapter translates it to its own flags."""

    #: Read anything, write nothing. Used for review, investigation and read_only tasks.
    READ_ONLY = "read_only"
    #: Write only inside the task's git worktree. The normal level for implementation.
    WORKSPACE_WRITE = "workspace_write"
    #: No sandbox. Never selected by the defaults; requires explicit opt-in.
    FULL = "full"


@dataclass(frozen=True, slots=True)
class EngineRoute:
    engine: Engine
    fallbacks: tuple[Engine, ...] = ()


@dataclass(frozen=True, slots=True)
class EnginePolicy:
    """How a single run is allowed to behave, independent of which engine runs it."""

    access: AccessLevel
    max_turns: int
    timeout_s: int
    deny_rules: tuple[str, ...] = ()
    #: Only ever true when a routing.toml explicitly asks for it. Adapters refuse to
    #: pass their `--dangerously-*` flags unless this is set.
    allow_dangerous: bool = False

    @property
    def effective_access(self) -> AccessLevel:
        """The access level adapters are allowed to act on.

        `resolve_policy` only produces FULL alongside the opt-in, but adapters read this
        instead of `access` so that a hand-built or deserialised policy cannot turn an
        unsandboxed run on by setting one field.
        """
        if self.access is AccessLevel.FULL and not self.allow_dangerous:
            return AccessLevel.WORKSPACE_WRITE
        return self.access


#: Turn and wall-clock budgets per risk level. These are the main cost controls.
DEFAULT_BUDGETS: Mapping[Risk, tuple[int, int]] = {
    Risk.READ_ONLY: (12, 600),
    Risk.NORMAL: (40, 1800),
    Risk.HIGH: (20, 900),
}

#: Applied to every run that supports deny rules, on top of the OS sandbox.
DEFAULT_DENY_RULES: tuple[str, ...] = (
    "Bash(git push:*)",
    "Bash(gh pr merge:*)",
    "Bash(sudo:*)",
    "Bash(rm -rf /*)",
)


class RoutingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RoutingTable:
    routes: Mapping[TaskKind, EngineRoute]
    budgets: Mapping[Risk, tuple[int, int]]
    deny_rules: tuple[str, ...]
    dangerous_engines: frozenset[Engine]

    def describe(self) -> list[dict[str, object]]:
        """Human/LLM readable form of the table, surfaced through `orch_engines`."""
        return [
            {
                "kind": kind.value,
                "engine": route.engine.value,
                "fallbacks": [engine.value for engine in route.fallbacks],
                "writes": kind in WRITING_KINDS,
            }
            for kind, route in self.routes.items()
        ]


DEFAULT_TABLE = RoutingTable(
    routes={},
    budgets=DEFAULT_BUDGETS,
    deny_rules=DEFAULT_DENY_RULES,
    dangerous_engines=frozenset(),
)


def load_routing_table(path: Path | None) -> RoutingTable:
    """Load `routing.toml`. Routes come only from the file, which `orch init` writes.

    There is deliberately no built-in engine preference: without the file every kind is
    unrouted, and only an explicit engine request can run. Budgets and deny rules keep
    their defaults, so a file that sets only routes does not have to restate them.
    """
    if path is None or not path.exists():
        return DEFAULT_TABLE

    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    routes: dict[TaskKind, EngineRoute] = {}
    for name, entry in (raw.get("kinds") or {}).items():
        kind = _parse_enum(TaskKind, name, f"kinds.{name}")
        engine = _parse_enum(Engine, entry.get("engine"), f"kinds.{name}.engine")
        fallbacks = tuple(
            _parse_enum(Engine, value, f"kinds.{name}.fallbacks")
            for value in entry.get("fallbacks", [])
        )
        routes[kind] = EngineRoute(engine, fallbacks)

    budgets = dict(DEFAULT_BUDGETS)
    for name, entry in (raw.get("risk") or {}).items():
        risk = _parse_enum(Risk, name, f"risk.{name}")
        current = budgets[risk]
        budgets[risk] = (
            int(entry.get("max_turns", current[0])),
            int(entry.get("timeout_s", current[1])),
        )

    deny_rules = tuple(raw.get("deny_rules", DEFAULT_DENY_RULES))
    dangerous = frozenset(
        _parse_enum(Engine, name, f"engines.{name}")
        for name, entry in (raw.get("engines") or {}).items()
        if entry.get("allow_dangerous", False)
    )
    return RoutingTable(
        routes=routes, budgets=budgets, deny_rules=deny_rules, dangerous_engines=dangerous
    )


def render_routing_toml(engines: Iterable[Engine]) -> str:
    """Draft a routing.toml that sends every kind to the installed engines.

    No engine is preferred: they are listed alphabetically, the same order the
    any-installed rescue uses, and the user reorders them per kind.
    """
    names = sorted(engine.value for engine in engines)
    if not names:
        raise RoutingError("no coding agent CLI is available on this machine")
    lines: list[str] = []
    for kind in TaskKind:
        fallbacks = ", ".join(f'"{name}"' for name in names[1:])
        lines += [f"[kinds.{kind.value}]", f'engine = "{names[0]}"', f"fallbacks = [{fallbacks}]", ""]
    return "\n".join(lines)


def configured_route(table: RoutingTable, kind: TaskKind) -> EngineRoute:
    route = table.routes.get(kind)
    if route is None:
        raise RoutingError(
            f"no route for {kind.value}: run `orch init` to write routing.toml, "
            "or pass an engine explicitly"
        )
    return route


def resolve_engine(
    kind: TaskKind,
    available: Iterable[Engine],
    requested: Engine | None = None,
    table: RoutingTable = DEFAULT_TABLE,
) -> Engine:
    """Pick the engine for `kind`, honouring an explicit request when it is usable.

    Raises RoutingError only when nothing at all is installed, which is worth failing
    loudly on rather than queueing a task that can never run.
    """
    usable = set(available)
    if not usable:
        raise RoutingError("no coding agent CLI is available on this machine")

    if requested is not None:
        if requested in usable:
            return requested
        raise RoutingError(
            f"engine {requested.value} was requested but is not available "
            f"(available: {', '.join(sorted(e.value for e in usable))})"
        )

    route = configured_route(table, kind)
    for candidate in (route.engine, *route.fallbacks):
        if candidate in usable:
            return candidate
    # The table's own fallbacks are exhausted; take any installed engine rather than
    # refusing work. Ordering keeps the choice deterministic.
    return sorted(usable, key=lambda engine: engine.value)[0]


def snapshot_workflow_routes(
    table: RoutingTable = DEFAULT_TABLE,
    overrides: Mapping[TaskKind, WorkflowRouteOverride] | None = None,
) -> tuple[WorkflowRoute, ...]:
    """Freeze a complete routing table for a run or batch.

    Auto overrides put their custom primary ahead of the current configured chain.
    Resolution still has the ordinary deterministic any-installed rescue, represented
    by the persisted auto mode rather than by whichever engines happen to be installed
    when the workflow is created.
    """
    supplied = overrides or {}
    unknown = set(supplied) - set(TaskKind)
    if unknown:
        names = ", ".join(sorted(str(kind) for kind in unknown))
        raise RoutingError(f"unknown workflow route kinds: {names}")

    snapshots: list[WorkflowRoute] = []
    for kind in TaskKind:
        configured = table.routes.get(kind)
        override = supplied.get(kind)
        if override is None:
            configured = configured_route(table, kind)
            primary = configured.engine
            mode = FallbackMode.AUTO
            candidates = configured.fallbacks
        elif override.fallback_mode is FallbackMode.MANUAL:
            primary = override.primary
            mode = FallbackMode.MANUAL
            candidates = override.fallbacks
        else:
            primary = override.primary
            mode = FallbackMode.AUTO
            candidates = (configured.engine, *configured.fallbacks) if configured else ()

        fallbacks = tuple(
            candidate
            for index, candidate in enumerate(candidates)
            if candidate != primary and candidate not in candidates[:index]
        )
        snapshots.append(
            WorkflowRoute(
                kind=kind,
                primary=primary,
                fallback_mode=mode,
                fallbacks=fallbacks,
            )
        )
    return tuple(snapshots)


def resolve_workflow_engine(
    route: WorkflowRoute,
    available: Iterable[Engine],
) -> Engine:
    """Resolve against a persisted route without consulting current configuration."""
    usable = set(available)
    if not usable:
        raise RoutingError("no coding agent CLI is available on this machine")

    for candidate in (route.primary, *route.fallbacks):
        if candidate in usable:
            return candidate
    if route.fallback_mode is FallbackMode.AUTO:
        return sorted(usable, key=lambda engine: engine.value)[0]

    configured = ", ".join(
        engine.value for engine in (route.primary, *route.fallbacks)
    )
    available_names = ", ".join(sorted(engine.value for engine in usable))
    raise RoutingError(
        f"manual route for {route.kind.value} is exhausted "
        f"(configured: {configured}; available: {available_names})"
    )


def resolve_access(kind: TaskKind, risk: Risk) -> AccessLevel:
    """Read-only unless the task both intends to write and is allowed to.

    `risk=high` is planning-only in this orchestrator, so it stays read-only however
    write-shaped the kind is.
    """
    if risk in (Risk.READ_ONLY, Risk.HIGH):
        return AccessLevel.READ_ONLY
    if kind in WRITING_KINDS:
        return AccessLevel.WORKSPACE_WRITE
    return AccessLevel.READ_ONLY


def resolve_policy(
    kind: TaskKind,
    risk: Risk,
    engine: Engine,
    table: RoutingTable = DEFAULT_TABLE,
) -> EnginePolicy:
    max_turns, timeout_s = table.budgets.get(risk, DEFAULT_BUDGETS[risk])
    policy = EnginePolicy(
        access=resolve_access(kind, risk),
        max_turns=max_turns,
        timeout_s=timeout_s,
        deny_rules=table.deny_rules,
        allow_dangerous=engine in table.dangerous_engines,
    )
    if policy.allow_dangerous and policy.access is AccessLevel.WORKSPACE_WRITE:
        return replace(policy, access=AccessLevel.FULL)
    return policy


def _parse_enum[T: StrEnum](enum: type[T], value: object, where: str) -> T:
    if not isinstance(value, str):
        raise RoutingError(f"{where}: expected a string, got {value!r}")
    try:
        return enum(value)
    except ValueError:
        allowed = ", ".join(member.value for member in enum)
        raise RoutingError(f"{where}: {value!r} is not one of {allowed}") from None


def resolve_allowed_tools(risk: Risk) -> list[str]:
    if risk == Risk.READ_ONLY:
        return ["Read", "Grep", "Glob", "Bash"]
    if risk == Risk.HIGH:
        return ["Read", "Grep", "Glob", "Bash"]
    return ["Read", "Edit", "Write", "Grep", "Glob", "Bash"]
