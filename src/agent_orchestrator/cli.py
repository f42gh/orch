from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

from agent_orchestrator import __version__
from agent_orchestrator.command_installer import (
    SUPPORTED_COMMAND_LOCALES,
    CommandInstallError,
    install_command,
)
from agent_orchestrator.config import Config, load_config
from agent_orchestrator.db import TaskStore
from agent_orchestrator.daemon import add_arguments as add_daemon_arguments
from agent_orchestrator.daemon import run as run_daemon_command
from agent_orchestrator.dispatch import DispatchError, dispatch_task, existing_repo
from agent_orchestrator.engines import probe_all
from agent_orchestrator.models import (
    Engine,
    FallbackMode,
    Priority,
    Risk,
    TaskKind,
    WorkflowRouteOverride,
    WorkflowTaskRequest,
    WorkflowType,
)
from agent_orchestrator.parsing import (
    load_tasks_file,
    parse_engine,
    parse_iso_datetime,
    parse_kind,
    require_engine,
    route_overrides_from_flags,
)
from agent_orchestrator.router import load_routing_table
from agent_orchestrator.stats import Stats, Totals, build_stats
from agent_orchestrator.usage import UsageReport, collect_usage
from agent_orchestrator.views import (
    dispatched_batch,
    dispatched_task,
    task_detail,
    workflow_details,
    workflow_summary,
)
from agent_orchestrator.workflows import (
    WorkflowError,
    close_workflow,
    create_run,
    dispatch_batch,
    dispatch_run,
    list_workflows,
    show_workflow,
)


@dataclass
class Context:
    """What every subcommand handler is handed.

    The store is built on first use, not up front. Constructing a `TaskStore` creates
    the runtime directories and the SQLite schema as a side effect, and two commands
    answer without a database at all: `usage` reads files the engines wrote, and
    `start` refuses a non-TTY before anything exists. Making the store lazy is what
    keeps `agentctl usage` from conjuring a runtime root — it used to depend on those
    two commands returning early, before the line that built the store.
    """

    config: Config
    _store: TaskStore | None = None

    @property
    def store(self) -> TaskStore:
        if self._store is None:
            self._store = TaskStore(self.config)
        return self._store


#: Shown under `agentctl --help`. The route syntax is the one thing a reader cannot
#: guess from a metavar, and until now it only appeared in the README.
EPILOG = """examples:
  orch engines                        what this machine has, and how kinds route
  orch usage                          how much of each subscription is left
  orch add --repo ~/dev/app --task "update the README"
  orch dispatch --repo ~/dev/app --task "review the parser" --kind review
  orch run create --repo ~/dev/app --route implement=codex --fallback implement=claude,agy
  orch run dispatch run-0001 --task "add the parser" --kind implement
  orch batch dispatch --repo ~/dev/app --route review=grok --tasks-file tasks.json
  orch stats --group-by engine --since 2026-08-01

A --route sets one kind's primary engine. A --fallback needs a matching --route and
is strict: only those engines are tried, in that order. Omit it to snapshot the
automatic fallbacks instead."""

#: `agy` is accepted everywhere `antigravity` is, and always serializes back as the
#: long name. Listing both keeps `--help` honest about what the shell will accept.
ENGINE_CHOICES = [*(engine.value for engine in Engine), "agy"]


def _add_json_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", help="print the result as one JSON object"
    )


def _add_task_shape_arguments(parser: argparse.ArgumentParser) -> None:
    """The task's own attributes, shared by every command that creates one."""
    parser.add_argument(
        "--kind",
        choices=[kind.value for kind in TaskKind],
        default=TaskKind.IMPLEMENT.value,
        help="what the task is for; selects the engine unless --engine is given",
    )
    parser.add_argument(
        "--risk",
        choices=[risk.value for risk in Risk],
        default=Risk.NORMAL.value,
        help="how much the engine may touch; read_only forbids writes entirely",
    )
    parser.add_argument(
        "--priority",
        choices=[priority.value for priority in Priority],
        default=Priority.NORMAL.value,
        help="queue order for the daemon when several tasks are waiting",
    )
    parser.add_argument("--parent", default=None, help="group this task under another task id")
    parser.add_argument(
        "--base-ref",
        default=None,
        metavar="REF",
        help="branch, tag or commit to branch the worktree from (default: current HEAD)",
    )


def _add_task_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo", required=True, metavar="PATH", help="the git checkout to work in"
    )
    parser.add_argument(
        "--task", required=True, metavar="TEXT", help="what the engine should do"
    )
    parser.add_argument(
        "--engine",
        default=None,
        choices=ENGINE_CHOICES,
        help="override the routed engine; agy is an alias for antigravity",
    )
    _add_task_shape_arguments(parser)


def _add_route_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--route",
        action="append",
        default=[],
        metavar="KIND=PRIMARY",
        help="set one kind's primary engine; repeat for multiple kinds",
    )
    parser.add_argument(
        "--fallback",
        action="append",
        default=[],
        metavar="KIND=E1,E2",
        help=(
            "set a strict ordered fallback list for a matching --route; "
            "omit it to snapshot automatic fallbacks"
        ),
    )


def _runtime_root_parent() -> argparse.ArgumentParser:
    """`--runtime-root` after the subcommand as well as before it.

    `agentd` and `agentapi` take it after their subcommand, so `agentctl` accepting it
    only before one was a difference nobody chose. The default is SUPPRESS rather than
    None: an argparse subparser writes its defaults over values the main parser already
    set, so a plain default here would erase `agentctl --runtime-root X list`.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--runtime-root",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="override the runtime root (default: $AGENT_ORCHESTRATOR_RUNTIME_ROOT "
        "or ~/agent-runtime)",
    )
    return parent


def build_parser() -> argparse.ArgumentParser:
    runtime_root = _runtime_root_parent()
    # No prog=: the same parser is reached as `orch` and as the `agentctl` alias, and
    # the usage line should name whichever the user actually typed.
    parser = argparse.ArgumentParser(
        description="Hand coding work to another agent CLI and review what comes back.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"orch {__version__}")
    parser.add_argument(
        "--runtime-root",
        default=None,
        metavar="PATH",
        help="override the runtime root (default: $AGENT_ORCHESTRATOR_RUNTIME_ROOT "
        "or ~/agent-runtime)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return subparsers.add_parser(name, parents=[runtime_root], help=help_text)

    add_parser = add("add", "add a task to the queue without starting it")
    add_parser.set_defaults(func=_handle_add)
    _add_task_arguments(add_parser)
    _add_json_argument(add_parser)

    dispatch_parser = add(
        "dispatch", "add a task and start its detached worker immediately"
    )
    dispatch_parser.set_defaults(func=_handle_dispatch)
    _add_task_arguments(dispatch_parser)
    _add_json_argument(dispatch_parser)

    list_parser = add("list", "list tasks")
    list_parser.set_defaults(func=_handle_list)
    _add_json_argument(list_parser)

    stats_parser = add("stats", "summarize task outcomes and usage")
    stats_parser.set_defaults(func=_handle_stats)
    _add_json_argument(stats_parser)
    stats_parser.add_argument(
        "--repo", default=None, metavar="PATH", help="only tasks for this checkout"
    )
    stats_parser.add_argument(
        "--workflow",
        dest="workflow_id",
        default=None,
        metavar="ID",
        help="only tasks belonging to this Run or Batch",
    )
    stats_parser.add_argument(
        "--engine",
        default=None,
        choices=ENGINE_CHOICES,
        help="only tasks that ran on this engine; agy is an alias for antigravity",
    )
    stats_parser.add_argument(
        "--kind",
        choices=[kind.value for kind in TaskKind],
        default=None,
        help="only tasks of this kind",
    )
    stats_parser.add_argument(
        "--since", metavar="ISO", default=None, help="only tasks created at or after this time"
    )
    stats_parser.add_argument(
        "--until", metavar="ISO", default=None, help="only tasks created before this time"
    )
    stats_parser.add_argument(
        "--group-by",
        choices=["engine", "kind", "model", "status", "repo"],
        default=None,
        help="also print a per-group breakdown table",
    )

    engines_parser = add("engines", "show installed engines and the routing table")
    engines_parser.set_defaults(func=_handle_engines)
    _add_json_argument(engines_parser)

    usage_parser = add(
        "usage", "show each engine's own account quota reading and when it resets"
    )
    usage_parser.set_defaults(func=_handle_usage)
    _add_json_argument(usage_parser)

    show_parser = add("show", "show task details")
    show_parser.set_defaults(func=_handle_show)
    show_parser.add_argument("task_id", metavar="TASK_ID", help="the task to describe")
    _add_json_argument(show_parser)

    start_parser = add("start", "interactively create a persistent Run or one-shot Batch")
    start_parser.set_defaults(func=_handle_start)
    _add_json_argument(start_parser)

    run_parser = add("run", "manage persistent Run workflows")
    run_parser.set_defaults(func=_handle_run)
    run_subparsers = run_parser.add_subparsers(dest="run_command", required=True)

    def add_run(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = run_subparsers.add_parser(name, parents=[runtime_root], help=help_text)
        _add_json_argument(sub)
        return sub

    run_create = add_run("create", "create an open Run")
    run_create.add_argument(
        "--repo", required=True, metavar="PATH", help="the git checkout this Run works in"
    )
    _add_route_arguments(run_create)

    run_dispatch = add_run("dispatch", "add and start one task under an open Run")
    run_dispatch.add_argument("run_id", metavar="RUN_ID", help="the open Run to add to")
    run_dispatch.add_argument(
        "--task", required=True, metavar="TEXT", help="what the engine should do"
    )
    _add_task_shape_arguments(run_dispatch)

    add_run("list", "list Run workflows")
    run_show = add_run("show", "show one Run and its tasks")
    run_show.add_argument("run_id", metavar="RUN_ID", help="the Run to describe")
    run_close = add_run("close", "close a Run to further dispatch")
    run_close.add_argument("run_id", metavar="RUN_ID", help="the Run to close")

    batch_parser = add("batch", "manage sealed Batch workflows")
    batch_parser.set_defaults(func=_handle_batch)
    batch_subparsers = batch_parser.add_subparsers(dest="batch_command", required=True)

    def add_batch(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = batch_subparsers.add_parser(name, parents=[runtime_root], help=help_text)
        _add_json_argument(sub)
        return sub

    batch_dispatch = add_batch(
        "dispatch", "validate, persist, and start a complete independent task set"
    )
    batch_dispatch.add_argument(
        "--repo", required=True, metavar="PATH", help="the git checkout this Batch works in"
    )
    _add_route_arguments(batch_dispatch)
    batch_dispatch.add_argument(
        "--tasks-file",
        required=True,
        metavar="PATH",
        help="JSON array of task objects; use - to read stdin",
    )

    add_batch("list", "list Batch workflows")
    batch_show = add_batch("show", "show one Batch and its tasks")
    batch_show.add_argument("batch_id", metavar="BATCH_ID", help="the Batch to describe")

    daemon_parser = add("daemon", "run queued tasks (the worker loop)")
    daemon_parser.set_defaults(func=_handle_daemon)
    add_daemon_arguments(daemon_parser)

    # `api` and `mcp` declare their flags here rather than importing them from the
    # modules that implement them: those modules import fastapi and mcp at the top, and
    # both are optional extras. Sharing the definitions would make `orch --help` fail on
    # an install that only wanted the CLI. The handlers import lazily for the same reason.
    api_parser = add("api", "serve the local HTTP API the React UI reads")
    api_parser.set_defaults(func=_handle_api)
    api_parser.add_argument(
        "--host", default="127.0.0.1", metavar="ADDR", help="bind address (default: 127.0.0.1)"
    )
    api_parser.add_argument(
        "--port", type=int, default=8765, metavar="PORT", help="bind port (default: 8765)"
    )

    mcp_parser = add("mcp", "serve the MCP control plane over stdio")
    mcp_parser.set_defaults(func=_handle_mcp)
    mcp_parser.add_argument(
        "--routing", default=None, metavar="PATH", help="override the routing.toml path"
    )

    install_parser = add(
        "install-claude-command", "install the bundled /orch command for Claude Code"
    )
    install_parser.set_defaults(func=_handle_install)
    install_parser.add_argument(
        "--target",
        default=None,
        metavar="PATH",
        help="override ~/.claude/commands/orch.md",
    )
    install_parser.add_argument(
        "--locale",
        choices=SUPPORTED_COMMAND_LOCALES,
        default="en",
        help="language for the command template (default: en)",
    )
    install_parser.add_argument(
        "--force", action="store_true", help="back up and replace a different existing file"
    )
    return parser


def _print_json(payload: object, *, stream: TextIO | None = None) -> None:
    print(json.dumps(payload, ensure_ascii=False), file=stream or sys.stdout)


def _display_width(text: str) -> int:
    """Terminal columns, not code points.

    Task descriptions here are often Japanese, and a CJK character occupies two
    columns. Padding by `len()` leaves those rows visibly short.
    """
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _print_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    stream: TextIO | None = None,
    indent: str = "",
) -> None:
    """Print aligned columns.

    Every table used to be written out by hand at its call site, each with its own
    header string and its own row f-string, which is why no two of them agreed on
    alignment or on how to spell an absent value. Machine callers have `--json` now,
    so the human output is free to be padded for reading.
    """
    out = stream or sys.stdout
    widths = [_display_width(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], _display_width(cell))
    for line in (headers, *rows):
        padded = [
            cell + " " * (widths[index] - _display_width(cell))
            for index, cell in enumerate(line)
        ]
        print((indent + "  ".join(padded)).rstrip(), file=out)


def _format_cost(totals: Totals) -> str:
    """Never print the total alone: codex and antigravity report no cost at all.

    A bare figure reads as the whole bill, so the reported fraction always travels with
    it, and the engines responsible for the gap are named whenever there is one.
    """
    coverage = f"{totals.cost_reported_tasks}/{totals.terminal} terminal tasks reported"
    if totals.cost_unreported_tasks:
        missing = ", ".join(totals.engines_without_cost) or "unknown"
        coverage = f"{coverage}; no cost from {missing}"
    return f"{totals.cost_usd:.4f} ({coverage})"


def _format_rate(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "-"


def _format_ms_seconds(value: int | None) -> str:
    return f"{value / 1000:.1f}" if value is not None else "-"


def _format_window_minutes(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value and value % (24 * 60) == 0:
        return f"{value // (24 * 60)}d"
    if value and value % 60 == 0:
        return f"{value // 60}h"
    return f"{value}m"


def _format_quota_reset(value: datetime | None) -> str:
    if value is None:
        return "-"
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


def _print_stats(stats: Stats, *, stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    totals = stats.totals
    statuses = ", ".join(
        f"{status}={count}" for status, count in totals.by_status.items()
    ) or "-"
    print(f"tasks: {totals.tasks}", file=out)
    print(f"by_status: {statuses}", file=out)
    print(f"terminal: {totals.terminal}", file=out)
    print(f"in_flight: {totals.in_flight}", file=out)
    print(f"completed: {totals.completed}", file=out)
    print(f"failed: {totals.failed}", file=out)
    print(f"cancelled: {totals.cancelled}", file=out)
    print(f"success_rate: {_format_rate(totals.success_rate)}", file=out)
    print(f"cost_usd: {_format_cost(totals)}", file=out)
    print(
        f"tokens: {totals.tokens.total} "
        f"({totals.tokens_reported_tasks}/{totals.tasks} tasks reported; "
        f"input={totals.tokens.input_tokens}, output={totals.tokens.output_tokens}, "
        f"cache_read={totals.tokens.cache_read_tokens}, "
        f"cache_write={totals.tokens.cache_write_tokens}, "
        f"reasoning={totals.tokens.reasoning_tokens})",
        file=out,
    )
    print(f"engine_s_total: {_format_ms_seconds(totals.engine_ms_total)}", file=out)
    print(f"engine_s_p50: {_format_ms_seconds(totals.engine_ms_p50)}", file=out)
    print(f"engine_s_p95: {_format_ms_seconds(totals.engine_ms_p95)}", file=out)
    queue_wait = (
        f"{totals.queue_wait_s_p50:.1f}"
        if totals.queue_wait_s_p50 is not None
        else "-"
    )
    print(f"queue_wait_s_p50: {queue_wait}", file=out)
    print(f"files_changed: {totals.files_changed}", file=out)
    print(f"insertions: {totals.insertions}", file=out)
    print(f"deletions: {totals.deletions}", file=out)

    for snapshot in stats.quota_snapshots.values():
        plan = snapshot.plan_type or "-"
        print(
            f"quota: {snapshot.engine.value} {snapshot.used_pct:.1f}% of a "
            f"{_format_window_minutes(snapshot.window_minutes)} window "
            f"(plan={plan}, resets {_format_quota_reset(snapshot.resets_at)})",
            file=out,
        )

    if stats.group_by is None:
        return
    print(file=out)
    _print_table(
        [
            stats.group_by,
            "tasks",
            "terminal",
            "completed",
            "failed",
            "cancelled",
            "in_flight",
            "success_rate",
            "cost_usd",
            "tokens",
            "engine_s_total",
            "engine_s_p50",
            "engine_s_p95",
            "queue_wait_s_p50",
            "files_changed",
            "insertions",
            "deletions",
        ],
        [
            [
                key,
                str(group.tasks),
                str(group.terminal),
                str(group.completed),
                str(group.failed),
                str(group.cancelled),
                str(group.in_flight),
                _format_rate(group.success_rate),
                _format_cost(group),
                str(group.tokens.total),
                _format_ms_seconds(group.engine_ms_total),
                _format_ms_seconds(group.engine_ms_p50),
                _format_ms_seconds(group.engine_ms_p95),
                (
                    f"{group.queue_wait_s_p50:.1f}"
                    if group.queue_wait_s_p50 is not None
                    else "-"
                ),
                str(group.files_changed),
                str(group.insertions),
                str(group.deletions),
            ]
            for key, group in stats.groups.items()
        ],
        stream=out,
    )


def _handle_stats(args: argparse.Namespace, ctx: Context) -> None:
    try:
        engine = parse_engine(args.engine)
        since = parse_iso_datetime(args.since, "--since")
        until = parse_iso_datetime(args.until, "--until")
        stats = build_stats(
            ctx.store.list_tasks(
                engine=engine,
                kind=TaskKind(args.kind) if args.kind else None,
                repo_path=Path(args.repo) if args.repo else None,
                since=since,
                until=until,
                workflow_id=args.workflow_id,
            ),
            group_by=args.group_by,
        )
    except (WorkflowError, ValueError) as exc:
        raise SystemExit(str(exc)) from None

    if args.json:
        _print_json(stats.describe())
    else:
        _print_stats(stats)


def _format_duration(delta: timedelta) -> str:
    """A rough gap, largest two units only: nobody schedules work by the second."""
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{max(seconds, 0)}s"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def _format_time_until(value: datetime | None, now: datetime) -> str:
    if value is None:
        return "-"
    remaining = value - now
    # A reset in the past means the reading predates it, so the percentage beside it
    # is spent quota that has since come back. Saying so beats printing a negative.
    return _format_duration(remaining) if remaining.total_seconds() > 0 else "elapsed"


def _format_age(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "-"
    return f"{_format_duration(timedelta(seconds=age_seconds))} ago"


def _print_usage(report: UsageReport, *, stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    now = report.collected_at
    rows: list[list[str]] = []
    for usage in report.engines:
        installed = "yes" if usage.installed else "no"
        plan = usage.plan or "-"
        age = _format_age(
            usage.age(now).total_seconds() if usage.age(now) is not None else None
        )
        if not usage.windows:
            rows.append([usage.engine.value, installed, plan, "-", "-", "-", "-", age, "-"])
            continue
        for window in usage.windows:
            label = window.label
            if window.window_minutes is not None:
                label = f"{label} ({_format_window_minutes(window.window_minutes)})"
            rows.append(
                [
                    usage.engine.value,
                    installed,
                    plan,
                    label,
                    f"{window.used_pct:.1f}%" if window.used_pct is not None else "-",
                    _format_quota_reset(window.resets_at),
                    _format_time_until(window.resets_at, now),
                    age,
                    window.detail or "-",
                ]
            )
    _print_table(
        ["engine", "installed", "plan", "window", "used", "resets_at", "in", "observed", "detail"],
        rows,
        stream=out,
    )

    footer = [
        f"source: {usage.engine.value} {usage.source}"
        for usage in report.engines
        if usage.source
    ] + [
        # The elapsed reset is already in the table; what it means for the percentage
        # beside it is not, and that is the part someone reading a quota acts on.
        f"stale: {usage.engine.value} a window reset after this reading, so real usage "
        f"is lower than shown"
        + (f" — {usage.refresh_hint}" if usage.refresh_hint else "")
        for usage in report.engines
        if usage.expired(now)
    ] + [
        f"note: {usage.engine.value} {note}"
        for usage in report.engines
        for note in usage.notes
    ]
    if footer:
        print(file=out)
        for line in footer:
            print(line, file=out)


def _print_workflow(payload: Mapping[str, Any], *, stream: TextIO | None = None) -> None:
    out = stream or sys.stdout
    print(f"{payload.get('type', 'workflow')} {payload.get('workflow_id', '?')}", file=out)
    print(f"status: {payload.get('status', '-')}", file=out)
    print(f"repo: {payload.get('repo', '-')}", file=out)
    totals = payload.get("totals")
    if isinstance(totals, Mapping):
        coverage = (
            f"{totals.get('cost_reported_tasks', 0)}/{totals.get('terminal', 0)} "
            "terminal tasks reported"
        )
        if int(totals.get("cost_unreported_tasks", 0)):
            engines_without_cost = totals.get("engines_without_cost") or []
            missing = ", ".join(str(engine) for engine in engines_without_cost) or "unknown"
            coverage = f"{coverage}; no cost from {missing}"
        print(
            f"totals: {totals.get('tasks', 0)} tasks; "
            f"{totals.get('completed', 0)} completed; "
            f"{totals.get('failed', 0)} failed; "
            f"{totals.get('cancelled', 0)} cancelled; "
            f"cost_usd {float(totals.get('cost_usd', 0.0)):.4f} ({coverage})",
            file=out,
        )
    for route in payload.get("routes") or []:
        kind = route["kind"]
        primary = route["primary"]
        fallbacks = route["fallbacks"]
        mode = route["fallback_mode"]
        chain = ",".join(fallbacks) if fallbacks else "-"
        print(f"route {kind}: {primary} (fallback {mode}: {chain})", file=out)
    tasks = payload.get("tasks") or []
    print(f"tasks: {len(tasks)}", file=out)
    _print_table(
        ["task_id", "engine", "task"],
        [
            [
                str(task.get("task_id", "-")),
                str(task.get("engine") or "-"),
                str(task.get("task", ""))[:50]
                + (
                    f"  spawn_error: {task['spawn_error']}"
                    if task.get("spawn_error")
                    else ""
                ),
            ]
            for task in tasks
        ],
        stream=out,
        indent="  ",
    )


def _print_workflow_table(
    workflows: Sequence[Mapping[str, Any]], *, stream: TextIO | None = None
) -> None:
    _print_table(
        ["workflow_id", "type", "status", "repo"],
        [
            [
                str(workflow.get("workflow_id", "-")),
                str(workflow.get("type", "-")),
                str(workflow.get("status", "-")),
                str(workflow.get("repo", "-")),
            ]
            for workflow in workflows
        ],
        stream=stream,
    )


def _print_workflow_dispatch(
    payload: Mapping[str, Any], *, stream: TextIO | None = None
) -> None:
    out = stream or sys.stdout
    print(f"dispatched {payload['task_id']}", file=out)
    print(f"engine: {payload['engine']}", file=out)
    print(f"branch: {payload['branch']}", file=out)
    print(f"worker_pid: {payload['worker_pid']}", file=out)
    if payload.get("spawn_error"):
        print(f"spawn_error: {payload['spawn_error']}", file=out)


def _prompt(
    label: str,
    *,
    default: str | None,
    stdin: TextIO,
    prompt_output: TextIO,
) -> str:
    suffix = f" [{default}]" if default is not None else ""
    print(f"{label}{suffix}: ", end="", file=prompt_output, flush=True)
    raw = stdin.readline()
    if raw == "":
        raise WorkflowError("interactive input ended before the workflow was complete")
    value = raw.strip()
    return default if not value and default is not None else value


def _prompt_yes_no(
    label: str,
    *,
    default: bool,
    stdin: TextIO,
    prompt_output: TextIO,
) -> bool:
    raw = _prompt(
        f"{label} ({'Y/n' if default else 'y/N'})",
        default="",
        stdin=stdin,
        prompt_output=prompt_output,
    ).lower()
    if not raw:
        return default
    if raw not in {"y", "yes", "n", "no"}:
        raise WorkflowError("answer yes or no")
    return raw in {"y", "yes"}


def _interactive_routes(
    config: Config, *, stdin: TextIO, prompt_output: TextIO
) -> dict[TaskKind, WorkflowRouteOverride]:
    installed = probe_all(refresh=True)
    table = load_routing_table(config.routing_path)
    overrides: dict[TaskKind, WorkflowRouteOverride] = {}
    print("Installed engines:", file=prompt_output)
    for engine in Engine:
        capabilities = installed.get(engine)
        if capabilities is not None:
            print(
                f"  {engine.value}: {capabilities.version} ({capabilities.path})",
                file=prompt_output,
            )
    if not installed:
        print("  (none detected)", file=prompt_output)
    print("Automatic routes:", file=prompt_output)
    for entry in table.describe():
        chain = ",".join(entry["fallbacks"]) or "-"  # type: ignore[arg-type]
        print(f"  {entry['kind']}: {entry['engine']} -> {chain}", file=prompt_output)
    for kind in TaskKind:
        if not _prompt_yes_no(
            f"Override {kind.value}?",
            default=False,
            stdin=stdin,
            prompt_output=prompt_output,
        ):
            continue
        primary = require_engine(
            _prompt(
                f"Primary engine for {kind.value}",
                default=table.routes[kind].engine.value,
                stdin=stdin,
                prompt_output=prompt_output,
            )
        )
        mode = _prompt(
            f"Fallback mode for {kind.value} (auto/manual)",
            default="auto",
            stdin=stdin,
            prompt_output=prompt_output,
        ).lower()
        if mode == "auto":
            overrides[kind] = WorkflowRouteOverride(primary)
            continue
        if mode != "manual":
            raise WorkflowError("fallback mode must be auto or manual")
        raw = _prompt(
            f"Ordered fallbacks for {kind.value} (comma separated)",
            default=None,
            stdin=stdin,
            prompt_output=prompt_output,
        )
        parts = [item.strip() for item in raw.split(",")]
        if not parts or any(not item for item in parts):
            raise WorkflowError(
                f"fallbacks for {kind.value} must be a non-empty engine list"
            )
        chain = tuple(require_engine(item) for item in parts)
        try:
            overrides[kind] = WorkflowRouteOverride(primary, FallbackMode.MANUAL, chain)
        except ValueError as exc:
            raise WorkflowError(f"route {kind.value}: {exc}") from None
    return overrides


def _interactive_batch_tasks(
    *, stdin: TextIO, prompt_output: TextIO
) -> tuple[WorkflowTaskRequest, ...]:
    tasks: list[WorkflowTaskRequest] = []
    print("Enter independent tasks; leave task empty when finished.", file=prompt_output)
    while True:
        task = _prompt("Task", default="", stdin=stdin, prompt_output=prompt_output)
        if not task:
            break
        kind = parse_kind(
            _prompt(
                "Kind",
                default=TaskKind.IMPLEMENT.value,
                stdin=stdin,
                prompt_output=prompt_output,
            )
        )
        risk = Risk(
            _prompt(
                "Risk",
                default=Risk.NORMAL.value,
                stdin=stdin,
                prompt_output=prompt_output,
            )
        )
        priority = Priority(
            _prompt(
                "Priority",
                default=Priority.NORMAL.value,
                stdin=stdin,
                prompt_output=prompt_output,
            )
        )
        tasks.append(WorkflowTaskRequest(task, kind, risk, priority))
    if not tasks:
        raise WorkflowError("a batch must contain at least one task")
    return tuple(tasks)


def run_start_wizard(
    config: Config,
    store: TaskStore | None = None,
    *,
    as_json: bool = False,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    prompt_output: TextIO | None = None,
) -> dict[str, Any]:
    """Interactively create a Run or Batch, refusing non-TTY implicit input."""
    in_stream = stdin if stdin is not None else sys.stdin
    out_stream = stdout if stdout is not None else sys.stdout
    prompts = prompt_output if prompt_output is not None else sys.stderr
    if stdin is None and not sys.stdin.isatty():
        raise SystemExit(
            "agentctl start requires an interactive TTY; use 'agentctl run create' or "
            "'agentctl batch dispatch --tasks-file PATH' for non-interactive use"
        )
    task_store = store or TaskStore(config)
    try:
        repo = _prompt(
            "Repository",
            default=str(Path.cwd()),
            stdin=in_stream,
            prompt_output=prompts,
        )
        workflow_type = _prompt(
            "Workflow type (run/batch)",
            default=WorkflowType.RUN.value,
            stdin=in_stream,
            prompt_output=prompts,
        ).lower()
        if workflow_type not in {WorkflowType.RUN.value, WorkflowType.BATCH.value}:
            raise WorkflowError("workflow type must be run or batch")
        routes = _interactive_routes(config, stdin=in_stream, prompt_output=prompts)
        if workflow_type == WorkflowType.RUN.value:
            payload = workflow_details(
                config,
                create_run(config, task_store, repo=repo, routes=routes)
            )
        else:
            tasks = _interactive_batch_tasks(stdin=in_stream, prompt_output=prompts)
            payload = dispatched_batch(
                config,
                task_store,
                dispatch_batch(config, task_store, repo=repo, tasks=tasks, routes=routes),
            )
    except (WorkflowError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    if as_json:
        _print_json(payload, stream=out_stream)
    else:
        _print_workflow(payload, stream=out_stream)
    return payload


def _handle_run(args: argparse.Namespace, ctx: Context) -> None:
    config, store = ctx.config, ctx.store
    try:
        if args.run_command == "create":
            routes = route_overrides_from_flags(args.route, args.fallback)
            payload = workflow_details(
                config, create_run(config, store, repo=args.repo, routes=routes)
            )
        elif args.run_command == "dispatch":
            dispatched = dispatch_run(
                config,
                store,
                run_id=args.run_id,
                task=args.task,
                kind=TaskKind(args.kind),
                risk=Risk(args.risk),
                priority=Priority(args.priority),
                parent_id=args.parent,
                base_ref=args.base_ref,
            )
            payload = dispatched_task(config, dispatched)
        elif args.run_command == "list":
            workflows = [
                workflow_summary(workflow)
                for workflow in list_workflows(store, WorkflowType.RUN)
            ]
            if args.json:
                _print_json({"workflows": workflows})
            else:
                _print_workflow_table(workflows)
            return
        elif args.run_command == "show":
            details = show_workflow(store, args.run_id)
            if details.workflow_type is not WorkflowType.RUN:
                raise WorkflowError(f"workflow {args.run_id} is a batch, not a run")
            payload = workflow_details(config, details)
        else:
            details = close_workflow(store, args.run_id)
            payload = workflow_details(config, details)
    except (WorkflowError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    if args.json:
        _print_json(payload)
    elif args.run_command == "dispatch":
        _print_workflow_dispatch(payload)
    else:
        _print_workflow(payload)


def _handle_batch(args: argparse.Namespace, ctx: Context) -> None:
    config, store = ctx.config, ctx.store
    try:
        if args.batch_command == "dispatch":
            routes = route_overrides_from_flags(args.route, args.fallback)
            requests = load_tasks_file(args.tasks_file)
            payload = dispatched_batch(
                config,
                store,
                dispatch_batch(config, store, repo=args.repo, tasks=requests, routes=routes),
            )
        elif args.batch_command == "list":
            workflows = [
                workflow_summary(workflow)
                for workflow in list_workflows(store, WorkflowType.BATCH)
            ]
            if args.json:
                _print_json({"workflows": workflows})
            else:
                _print_workflow_table(workflows)
            return
        else:
            details = show_workflow(store, args.batch_id)
            if details.workflow_type is not WorkflowType.BATCH:
                raise WorkflowError(f"workflow {args.batch_id} is a run, not a batch")
            payload = workflow_details(config, details)
    except (WorkflowError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    if args.json:
        _print_json(payload)
    else:
        _print_workflow(payload)


def _handle_install(args: argparse.Namespace, ctx: Context) -> None:
    try:
        result = install_command(
            Path(args.target) if args.target else None,
            locale=args.locale,
            force=args.force,
        )
    except CommandInstallError as exc:
        raise SystemExit(str(exc)) from None
    if not result.changed:
        print(f"Claude command is already up to date: {result.path}")
        return
    print(f"installed Claude command: {result.path}")
    if result.backup_path is not None:
        print(f"backup: {result.backup_path}")


def _handle_add(args: argparse.Namespace, ctx: Context) -> None:
    try:
        engine = parse_engine(args.engine)
        repo_path = existing_repo(args.repo)
    except (DispatchError, WorkflowError) as exc:
        raise SystemExit(str(exc)) from None
    task = ctx.store.add_task(
        repo_path=repo_path,
        task=args.task,
        risk=Risk(args.risk),
        priority=Priority(args.priority),
        kind=TaskKind(args.kind),
        engine=engine,
        parent_id=args.parent,
        base_ref=args.base_ref,
    )
    if args.json:
        _print_json(task_detail(ctx.config, task))
        return
    print(f"added {task.id}")
    print(f"status: {task.status.value}")
    print(f"kind: {task.kind.value}")
    print(f"engine: {task.engine.value if task.engine else 'auto (chosen at run time)'}")
    print(f"repo: {task.repo_path}")


def _handle_dispatch(args: argparse.Namespace, ctx: Context) -> None:
    try:
        dispatched = dispatch_task(
            ctx.config,
            ctx.store,
            repo=args.repo,
            task=args.task,
            kind=TaskKind(args.kind),
            risk=Risk(args.risk),
            priority=Priority(args.priority),
            engine=parse_engine(args.engine),
            parent_id=args.parent,
            base_ref=args.base_ref,
        )
    except (DispatchError, WorkflowError) as exc:
        raise SystemExit(str(exc)) from None
    except OSError as exc:
        # The row is already written by the time a spawn can fail, so say so: a caller
        # who reads this as "nothing happened" and retries ends up with two tasks.
        raise SystemExit(
            f"could not start the worker: {exc}; the task is queued — find its id with "
            "'agentctl list' and start it with 'agentd run-task <id>'"
        ) from None
    if args.json:
        print(json.dumps(dispatched.describe(ctx.config), ensure_ascii=False))
    else:
        print(f"dispatched {dispatched.task.id}")
        print(f"engine: {dispatched.engine.value}")
        print(f"branch: {dispatched.branch}")
        print(f"worker_pid: {dispatched.worker_pid}")


def _handle_list(args: argparse.Namespace, ctx: Context) -> None:
    tasks = ctx.store.list_tasks()
    if args.json:
        _print_json({"tasks": [task_detail(ctx.config, task) for task in tasks]})
        return
    _print_table(
        ["task_id", "status", "kind", "engine", "risk", "priority", "cost", "short_task"],
        [
            [
                task.id,
                task.status.value,
                task.kind.value,
                task.engine.value if task.engine else "-",
                task.risk.value,
                task.priority.value,
                f"{task.cost_usd:.4f}" if task.cost_usd is not None else "-",
                task.task.replace("\n", " ")[:50],
            ]
            for task in tasks
        ],
    )


def _handle_engines(args: argparse.Namespace, ctx: Context) -> None:
    table = load_routing_table(ctx.config.routing_path)
    if args.json:
        print(
            json.dumps(
                {
                    "engines": [
                        capabilities.describe()
                        for capabilities in probe_all(refresh=True).values()
                    ],
                    "routing": table.describe(),
                    "kinds": [kind.value for kind in TaskKind],
                },
                ensure_ascii=False,
            )
        )
        return
    _print_table(
        ["engine", "version", "structured", "cost"],
        [
            [
                capabilities.engine.value,
                str(capabilities.version),
                str(capabilities.structured_output),
                str(capabilities.reports_cost),
            ]
            for capabilities in probe_all(refresh=True).values()
        ],
    )
    print()
    _print_table(
        ["kind", "engine", "fallbacks", "writes"],
        [
            [
                str(entry["kind"]),
                str(entry["engine"]),
                ",".join(entry["fallbacks"]) or "-",  # type: ignore[arg-type]
                str(entry["writes"]),
            ]
            for entry in table.describe()
        ],
    )


def _handle_usage(args: argparse.Namespace, ctx: Context) -> None:
    report = collect_usage()
    if args.json:
        _print_json(report.describe())
    else:
        _print_usage(report)


def _handle_show(args: argparse.Namespace, ctx: Context) -> None:
    task = ctx.store.get_task(args.task_id)
    if task is None:
        raise SystemExit(f"task not found: {args.task_id}")
    if args.json:
        _print_json(task_detail(ctx.config, task))
        return
    log_path = ctx.config.logs_dir / task.id
    print(f"task_id: {task.id}")
    print(f"task: {task.task}")
    print(f"status: {task.status.value}")
    print(f"kind: {task.kind.value}")
    print(f"engine: {task.engine.value if task.engine else '-'}")
    print(f"repo: {task.repo_path}")
    print(f"workspace_path: {task.workspace_path}")
    print(f"branch_name: {task.branch_name}")
    print(f"cost_usd: {task.cost_usd if task.cost_usd is not None else '-'}")
    print(f"exit_code: {task.exit_code if task.exit_code is not None else '-'}")
    print(f"log_path: {log_path}")
    print(f"result_summary: {task.result_summary}")
    print(f"diff_path: {log_path / 'diff.patch'}")
    if task.error:
        print(f"error: {task.error}")


def _handle_daemon(args: argparse.Namespace, ctx: Context) -> None:
    run_daemon_command(args)


def _handle_api(args: argparse.Namespace, ctx: Context) -> None:
    from agent_orchestrator import api

    api.run(args)


def _handle_mcp(args: argparse.Namespace, ctx: Context) -> None:
    # Nothing may print to stdout past this point: it is the MCP transport.
    from agent_orchestrator import mcp_server

    mcp_server.serve(args.runtime_root, args.routing)


def _handle_start(args: argparse.Namespace, ctx: Context) -> None:
    run_start_wizard(ctx.config, as_json=args.json)


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        args.func(args, Context(load_config(args.runtime_root)))
    except sqlite3.Error as exc:
        # A locked or unreadable runtime database is an operator problem, not a bug to
        # report as a traceback.
        raise SystemExit(f"runtime database error: {exc}") from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
