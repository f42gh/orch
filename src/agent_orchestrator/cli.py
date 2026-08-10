from __future__ import annotations

import argparse
from pathlib import Path

from agent_orchestrator.config import load_config
from agent_orchestrator.daemon import run_daemon
from agent_orchestrator.db import TaskStore
from agent_orchestrator.engines import probe_all
from agent_orchestrator.models import Engine, Priority, Risk, TaskKind
from agent_orchestrator.router import load_routing_table


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentctl")
    parser.add_argument("--runtime-root", default=None, help="override runtime root")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="add a task")
    add_parser.add_argument("--repo", required=True)
    add_parser.add_argument("--task", required=True)
    add_parser.add_argument(
        "--kind",
        choices=[kind.value for kind in TaskKind],
        default=TaskKind.IMPLEMENT.value,
        help="what the task is for; selects the engine unless --engine is given",
    )
    add_parser.add_argument(
        "--engine",
        choices=[engine.value for engine in Engine],
        default=None,
        help="override the engine the router would pick",
    )
    add_parser.add_argument("--risk", choices=[risk.value for risk in Risk], default=Risk.NORMAL.value)
    add_parser.add_argument(
        "--priority", choices=[priority.value for priority in Priority], default=Priority.NORMAL.value
    )
    add_parser.add_argument("--parent", default=None, help="group this task under another task id")
    add_parser.add_argument("--base-ref", default=None, help="branch, tag or commit to branch from")

    subparsers.add_parser("list", help="list tasks")
    subparsers.add_parser("engines", help="show installed engines and the routing table")

    show_parser = subparsers.add_parser("show", help="show task details")
    show_parser.add_argument("task_id")

    daemon_parser = subparsers.add_parser("daemon", help="run daemon")
    daemon_parser.add_argument("--once", action="store_true")
    daemon_parser.add_argument("--idle-sleep", type=float, default=2.0)
    daemon_parser.add_argument("--max-concurrency", type=int, default=2)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.runtime_root)
    store = TaskStore(config)

    if args.command == "add":
        task = store.add_task(
            repo_path=Path(args.repo),
            task=args.task,
            risk=Risk(args.risk),
            priority=Priority(args.priority),
            kind=TaskKind(args.kind),
            engine=Engine(args.engine) if args.engine else None,
            parent_id=args.parent,
            base_ref=args.base_ref,
        )
        print(f"added {task.id}")
        print(f"status: {task.status.value}")
        print(f"kind: {task.kind.value}")
        print(f"engine: {task.engine.value if task.engine else 'auto (chosen at run time)'}")
        print(f"repo: {task.repo_path}")
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
