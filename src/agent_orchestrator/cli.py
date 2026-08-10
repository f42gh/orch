from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from agent_orchestrator.command_installer import (
    SUPPORTED_COMMAND_LOCALES,
    CommandInstallError,
    install_command,
)
from agent_orchestrator.config import Config, load_config
from agent_orchestrator.daemon import run_daemon
from agent_orchestrator.db import TaskStore
from agent_orchestrator.dispatch import DispatchError, dispatch_task
from agent_orchestrator.engines import probe_all
from agent_orchestrator.models import (
    Engine,
    FallbackMode,
    Priority,
    Risk,
    Task,
    TaskKind,
    Workflow,
    WorkflowDetails,
    WorkflowRouteOverride,
    WorkflowTaskRequest,
    WorkflowType,
)
from agent_orchestrator.router import load_routing_table
from agent_orchestrator.workflows import (
    DispatchedBatch,
    DispatchedWorkflowTask,
    WorkflowError,
    close_workflow,
    create_run,
    dispatch_batch,
    dispatch_run,
    list_workflows,
    show_workflow,
)


ENGINE_ALIASES = {"agy": Engine.ANTIGRAVITY.value}


def _add_task_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument(
        "--kind",
        choices=[kind.value for kind in TaskKind],
        default=TaskKind.IMPLEMENT.value,
        help="what the task is for; selects the engine unless --engine is given",
    )
    parser.add_argument(
        "--engine",
        default=None,
        help="override the routed engine; accepts agy as an alias for antigravity",
    )
    parser.add_argument("--risk", choices=[risk.value for risk in Risk], default=Risk.NORMAL.value)
    parser.add_argument(
        "--priority", choices=[priority.value for priority in Priority], default=Priority.NORMAL.value
    )
    parser.add_argument("--parent", default=None, help="group this task under another task id")
    parser.add_argument("--base-ref", default=None, help="branch, tag or commit to branch from")


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


def _add_workflow_output_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print one JSON object")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentctl")
    parser.add_argument("--runtime-root", default=None, help="override runtime root")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="add a task to the queue without starting it")
    _add_task_arguments(add_parser)

    dispatch_parser = subparsers.add_parser(
        "dispatch", help="add a task and start its detached worker immediately"
    )
    _add_task_arguments(dispatch_parser)
    dispatch_parser.add_argument(
        "--json", action="store_true", help="print the created task as one JSON line"
    )

    subparsers.add_parser("list", help="list tasks")
    engines_parser = subparsers.add_parser(
        "engines", help="show installed engines and the routing table"
    )
    engines_parser.add_argument(
        "--json", action="store_true", help="print engines and routing as one JSON line"
    )

    show_parser = subparsers.add_parser("show", help="show task details")
    show_parser.add_argument("task_id")

    daemon_parser = subparsers.add_parser("daemon", help="run daemon")
    daemon_parser.add_argument("--once", action="store_true")
    daemon_parser.add_argument("--idle-sleep", type=float, default=2.0)
    daemon_parser.add_argument("--max-concurrency", type=int, default=2)

    start_parser = subparsers.add_parser(
        "start", help="interactively create a persistent Run or one-shot Batch"
    )
    _add_workflow_output_argument(start_parser)

    run_parser = subparsers.add_parser("run", help="manage persistent Run workflows")
    run_subparsers = run_parser.add_subparsers(dest="run_command", required=True)

    run_create = run_subparsers.add_parser("create", help="create an open Run")
    run_create.add_argument("--repo", required=True)
    _add_route_arguments(run_create)
    _add_workflow_output_argument(run_create)

    run_dispatch = run_subparsers.add_parser(
        "dispatch", help="add and start one task under an open Run"
    )
    run_dispatch.add_argument("run_id", metavar="RUN_ID")
    run_dispatch.add_argument("--task", required=True)
    run_dispatch.add_argument(
        "--kind", choices=[kind.value for kind in TaskKind], default=TaskKind.IMPLEMENT.value
    )
    run_dispatch.add_argument(
        "--risk", choices=[risk.value for risk in Risk], default=Risk.NORMAL.value
    )
    run_dispatch.add_argument(
        "--priority",
        choices=[priority.value for priority in Priority],
        default=Priority.NORMAL.value,
    )
    run_dispatch.add_argument("--parent", default=None)
    run_dispatch.add_argument("--base-ref", default=None)
    _add_workflow_output_argument(run_dispatch)

    run_list = run_subparsers.add_parser("list", help="list Run workflows")
    _add_workflow_output_argument(run_list)
    run_show = run_subparsers.add_parser("show", help="show one Run and its tasks")
    run_show.add_argument("run_id", metavar="RUN_ID")
    _add_workflow_output_argument(run_show)
    run_close = run_subparsers.add_parser("close", help="close a Run to further dispatch")
    run_close.add_argument("run_id", metavar="RUN_ID")
    _add_workflow_output_argument(run_close)

    batch_parser = subparsers.add_parser("batch", help="manage sealed Batch workflows")
    batch_subparsers = batch_parser.add_subparsers(dest="batch_command", required=True)
    batch_dispatch = batch_subparsers.add_parser(
        "dispatch", help="validate, persist, and start a complete independent task set"
    )
    batch_dispatch.add_argument("--repo", required=True)
    _add_route_arguments(batch_dispatch)
    batch_dispatch.add_argument(
        "--tasks-file",
        required=True,
        metavar="PATH",
        help="JSON array of task objects; use - to read stdin",
    )
    _add_workflow_output_argument(batch_dispatch)
    batch_list = batch_subparsers.add_parser("list", help="list Batch workflows")
    _add_workflow_output_argument(batch_list)
    batch_show = batch_subparsers.add_parser("show", help="show one Batch and its tasks")
    batch_show.add_argument("batch_id", metavar="BATCH_ID")
    _add_workflow_output_argument(batch_show)

    install_parser = subparsers.add_parser(
        "install-claude-command", help="install the bundled /orch command for Claude Code"
    )
    install_parser.add_argument("--target", default=None, help="override ~/.claude/commands/orch.md")
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


def _parse_engine(value: str) -> Engine:
    normalized = ENGINE_ALIASES.get(value.strip(), value.strip())
    try:
        return Engine(normalized)
    except ValueError:
        allowed = ", ".join([*(engine.value for engine in Engine), "agy"])
        raise WorkflowError(f"engine {value!r} is not one of: {allowed}") from None


def _parse_kind(value: str) -> TaskKind:
    try:
        return TaskKind(value.strip())
    except ValueError:
        allowed = ", ".join(kind.value for kind in TaskKind)
        raise WorkflowError(f"task kind {value!r} is not one of: {allowed}") from None


def _parse_assignment(value: str, *, shape: str) -> tuple[str, str]:
    if "=" not in value:
        raise WorkflowError(f"expected {shape}, got {value!r}")
    left, right = value.split("=", 1)
    if not left.strip() or not right.strip():
        raise WorkflowError(f"expected {shape}, got {value!r}")
    return left.strip(), right.strip()


def _route_overrides_from_flags(
    route_flags: Sequence[str], fallback_flags: Sequence[str]
) -> dict[TaskKind, WorkflowRouteOverride]:
    primaries: dict[TaskKind, Engine] = {}
    for raw in route_flags:
        kind_raw, engine_raw = _parse_assignment(raw, shape="KIND=PRIMARY")
        kind = _parse_kind(kind_raw)
        if kind in primaries:
            raise WorkflowError(f"duplicate --route for {kind.value}")
        primaries[kind] = _parse_engine(engine_raw)

    fallbacks: dict[TaskKind, tuple[Engine, ...]] = {}
    for raw in fallback_flags:
        kind_raw, engines_raw = _parse_assignment(raw, shape="KIND=E1,E2")
        kind = _parse_kind(kind_raw)
        if kind in fallbacks:
            raise WorkflowError(f"duplicate --fallback for {kind.value}")
        if kind not in primaries:
            raise WorkflowError(
                f"--fallback for {kind.value} requires a matching explicit --route"
            )
        parts = [part.strip() for part in engines_raw.split(",")]
        if not parts or any(not part for part in parts):
            raise WorkflowError(f"--fallback for {kind.value} must be a non-empty engine list")
        fallbacks[kind] = tuple(_parse_engine(part) for part in parts)

    overrides: dict[TaskKind, WorkflowRouteOverride] = {}
    for kind, primary in primaries.items():
        chain = fallbacks.get(kind)
        try:
            overrides[kind] = (
                WorkflowRouteOverride(primary)
                if chain is None
                else WorkflowRouteOverride(primary, FallbackMode.MANUAL, chain)
            )
        except ValueError as exc:
            raise WorkflowError(f"route {kind.value}: {exc}") from None
    return overrides


def _task_request_from_mapping(value: object, *, where: str) -> WorkflowTaskRequest:
    if not isinstance(value, Mapping):
        raise WorkflowError(f"{where} must be a JSON object")
    allowed = {"task", "kind", "risk", "priority", "parent_id", "base_ref"}
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise WorkflowError(f"{where} has unknown fields: {', '.join(unknown)}")
    task = value.get("task")
    if not isinstance(task, str) or not task.strip():
        raise WorkflowError(f"{where}.task must be a non-empty string")
    try:
        kind = TaskKind(value.get("kind", TaskKind.IMPLEMENT.value))
        risk = Risk(value.get("risk", Risk.NORMAL.value))
        priority = Priority(value.get("priority", Priority.NORMAL.value))
    except ValueError as exc:
        raise WorkflowError(f"{where}: {exc}") from None
    parent_id = value.get("parent_id")
    base_ref = value.get("base_ref")
    if parent_id is not None and not isinstance(parent_id, str):
        raise WorkflowError(f"{where}.parent_id must be a string or null")
    if base_ref is not None and not isinstance(base_ref, str):
        raise WorkflowError(f"{where}.base_ref must be a string or null")
    return WorkflowTaskRequest(
        task=task.strip(),
        kind=kind,
        risk=risk,
        priority=priority,
        parent_id=parent_id,
        base_ref=base_ref,
    )


def _load_tasks_file(path: str, *, stdin: TextIO | None = None) -> tuple[WorkflowTaskRequest, ...]:
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
        _task_request_from_mapping(item, where=f"tasks[{index}]")
        for index, item in enumerate(payload)
    )


def _workflow_summary(workflow: Workflow) -> dict[str, Any]:
    return {
        "workflow_id": workflow.id,
        "type": workflow.workflow_type.value,
        "status": workflow.status.value,
        "repo": str(workflow.repo_path),
        "created_at": workflow.created_at.isoformat(),
        "closed_at": workflow.closed_at.isoformat() if workflow.closed_at else None,
    }


def _task_summary(task: Task, *, ordinal: int) -> dict[str, Any]:
    return {
        "task_id": task.id,
        "ordinal": ordinal,
        "status": task.status.value,
        "task": task.task,
        "kind": task.kind.value,
        "engine": task.engine.value if task.engine else None,
        "risk": task.risk.value,
        "priority": task.priority.value,
        "parent_id": task.parent_id,
        "base_ref": task.base_ref,
        "branch": task.branch_name,
    }


def _workflow_details_payload(details: WorkflowDetails) -> dict[str, Any]:
    routes = [
        {
            "kind": route.kind.value,
            "primary": route.primary.value,
            "fallback_mode": route.fallback_mode.value,
            "fallbacks": [engine.value for engine in route.fallbacks],
        }
        for route in details.routes
    ]
    tasks = [
        _task_summary(task, ordinal=membership.ordinal)
        for membership, task in zip(details.memberships, details.tasks, strict=True)
    ]
    return {
        **_workflow_summary(details.workflow),
        "routes": routes,
        "task_ids": [task["task_id"] for task in tasks],
        "tasks": tasks,
    }


def _dispatched_task_payload(config: Config, item: DispatchedWorkflowTask) -> dict[str, Any]:
    return {
        **item.dispatched.describe(config),
        "workflow_id": item.workflow.id,
        "ordinal": item.membership.ordinal,
        "task": item.task.task,
        "spawn_error": item.spawn_error,
    }


def _dispatched_batch_payload(
    config: Config, store: TaskStore, batch: DispatchedBatch
) -> dict[str, Any]:
    details = show_workflow(store, batch.workflow.id)
    tasks = [_dispatched_task_payload(config, item) for item in batch.dispatched]
    return {
        **_workflow_details_payload(details),
        "task_ids": [task["task_id"] for task in tasks],
        "tasks": tasks,
    }


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _print_workflow(payload: Mapping[str, Any]) -> None:
    print(f"{payload.get('type', 'workflow')} {payload.get('workflow_id', '?')}")
    print(f"status: {payload.get('status', '-')}")
    print(f"repo: {payload.get('repo', '-')}")
    for route in payload.get("routes") or []:
        kind = route["kind"]
        primary = route["primary"]
        fallbacks = route["fallbacks"]
        mode = route["fallback_mode"]
        chain = ",".join(fallbacks) if fallbacks else "-"
        print(f"route {kind}: {primary} (fallback {mode}: {chain})")
    tasks = payload.get("tasks") or []
    print(f"tasks: {len(tasks)}")
    for task in tasks:
        line = (
            f"  {task.get('task_id', '-')}\t{task.get('engine', '-')}\t"
            f"{str(task.get('task', ''))[:50]}"
        )
        if task.get("spawn_error"):
            line += f"\tspawn_error: {task['spawn_error']}"
        print(line)


def _print_workflow_table(workflows: Sequence[Mapping[str, Any]]) -> None:
    print("workflow_id\ttype\tstatus\trepo")
    for workflow in workflows:
        print(
            f"{workflow.get('workflow_id', '-')}\t{workflow.get('type', '-')}\t"
            f"{workflow.get('status', '-')}\t{workflow.get('repo', '-')}"
        )


def _print_workflow_dispatch(payload: Mapping[str, Any]) -> None:
    print(f"dispatched {payload['task_id']}")
    print(f"engine: {payload['engine']}")
    print(f"branch: {payload['branch']}")
    print(f"worker_pid: {payload['worker_pid']}")
    if payload.get("spawn_error"):
        print(f"spawn_error: {payload['spawn_error']}")


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
        primary = _parse_engine(
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
        chain = tuple(_parse_engine(item) for item in parts)
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
        kind = _parse_kind(
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
            payload = _workflow_details_payload(
                create_run(config, task_store, repo=repo, routes=routes)
            )
        else:
            tasks = _interactive_batch_tasks(stdin=in_stream, prompt_output=prompts)
            payload = _dispatched_batch_payload(
                config,
                task_store,
                dispatch_batch(config, task_store, repo=repo, tasks=tasks, routes=routes),
            )
    except (WorkflowError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    if as_json:
        print(json.dumps(payload, ensure_ascii=False), file=out_stream)
    else:
        old_stdout = sys.stdout
        try:
            sys.stdout = out_stream  # type: ignore[assignment]
            _print_workflow(payload)
        finally:
            sys.stdout = old_stdout
    return payload


def _handle_run(config: Config, store: TaskStore, args: argparse.Namespace) -> None:
    try:
        if args.run_command == "create":
            routes = _route_overrides_from_flags(args.route, args.fallback)
            payload = _workflow_details_payload(
                create_run(config, store, repo=args.repo, routes=routes)
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
            payload = _dispatched_task_payload(config, dispatched)
        elif args.run_command == "list":
            workflows = [
                _workflow_summary(workflow)
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
            payload = _workflow_details_payload(details)
        else:
            details = close_workflow(store, args.run_id)
            payload = _workflow_details_payload(details)
    except (WorkflowError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    if args.json:
        _print_json(payload)
    elif args.run_command == "dispatch":
        _print_workflow_dispatch(payload)
    else:
        _print_workflow(payload)


def _handle_batch(config: Config, store: TaskStore, args: argparse.Namespace) -> None:
    try:
        if args.batch_command == "dispatch":
            routes = _route_overrides_from_flags(args.route, args.fallback)
            requests = _load_tasks_file(args.tasks_file)
            payload = _dispatched_batch_payload(
                config,
                store,
                dispatch_batch(config, store, repo=args.repo, tasks=requests, routes=routes),
            )
        elif args.batch_command == "list":
            workflows = [
                _workflow_summary(workflow)
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
            payload = _workflow_details_payload(details)
    except (WorkflowError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    if args.json:
        _print_json(payload)
    else:
        _print_workflow(payload)


def _handle_install(args: argparse.Namespace) -> None:
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


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "install-claude-command":
        _handle_install(args)
        return

    config = load_config(args.runtime_root)
    if args.command == "start":
        # Let the wizard reject a pipe/non-TTY before creating the runtime database.
        run_start_wizard(config, as_json=args.json)
        return
    store = TaskStore(config)

    if args.command == "add":
        try:
            engine = _parse_engine(args.engine) if args.engine else None
        except WorkflowError as exc:
            raise SystemExit(str(exc)) from None
        task = store.add_task(
            repo_path=Path(args.repo),
            task=args.task,
            risk=Risk(args.risk),
            priority=Priority(args.priority),
            kind=TaskKind(args.kind),
            engine=engine,
            parent_id=args.parent,
            base_ref=args.base_ref,
        )
        print(f"added {task.id}")
        print(f"status: {task.status.value}")
        print(f"kind: {task.kind.value}")
        print(f"engine: {task.engine.value if task.engine else 'auto (chosen at run time)'}")
        print(f"repo: {task.repo_path}")
        return

    if args.command == "dispatch":
        try:
            dispatched = dispatch_task(
                config,
                store,
                repo=args.repo,
                task=args.task,
                kind=TaskKind(args.kind),
                risk=Risk(args.risk),
                priority=Priority(args.priority),
                engine=_parse_engine(args.engine) if args.engine else None,
                parent_id=args.parent,
                base_ref=args.base_ref,
            )
        except (DispatchError, WorkflowError) as exc:
            raise SystemExit(str(exc)) from None
        if args.json:
            print(json.dumps(dispatched.describe(config), ensure_ascii=False))
        else:
            print(f"dispatched {dispatched.task.id}")
            print(f"engine: {dispatched.engine.value}")
            print(f"branch: {dispatched.branch}")
            print(f"worker_pid: {dispatched.worker_pid}")
        return

    if args.command == "list":
        print("task_id\tstatus\tkind\tengine\trisk\tpriority\tcost\tshort_task")
        for task in store.list_tasks():
            short = task.task.replace("\n", " ")[:50]
            cost = f"{task.cost_usd:.4f}" if task.cost_usd is not None else "-"
            print(
                f"{task.id}\t{task.status.value}\t{task.kind.value}\t"
                f"{task.engine.value if task.engine else '-'}\t{task.risk.value}\t"
                f"{task.priority.value}\t{cost}\t{short}"
            )
        return

    if args.command == "engines":
        table = load_routing_table(config.routing_path)
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
        print("engine\tversion\tstructured\tcost")
        for capabilities in probe_all(refresh=True).values():
            print(
                f"{capabilities.engine.value}\t{capabilities.version}\t"
                f"{capabilities.structured_output}\t{capabilities.reports_cost}"
            )
        print("\nkind\tengine\tfallbacks\twrites")
        for entry in table.describe():
            fallbacks = ",".join(entry["fallbacks"]) or "-"  # type: ignore[arg-type]
            print(f"{entry['kind']}\t{entry['engine']}\t{fallbacks}\t{entry['writes']}")
        return

    if args.command == "show":
        task = store.get_task(args.task_id)
        if task is None:
            raise SystemExit(f"task not found: {args.task_id}")
        log_path = config.logs_dir / task.id
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
        return

    if args.command == "daemon":
        import asyncio

        asyncio.run(
            run_daemon(
                store,
                once=args.once,
                idle_sleep=args.idle_sleep,
                max_concurrency=args.max_concurrency,
            )
        )
        return

    if args.command == "run":
        _handle_run(config, store, args)
        return
    if args.command == "batch":
        _handle_batch(config, store, args)


if __name__ == "__main__":
    main()
