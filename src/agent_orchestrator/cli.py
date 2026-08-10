from __future__ import annotations

import argparse
from pathlib import Path

from agent_orchestrator.config import load_config
from agent_orchestrator.daemon import run_daemon
from agent_orchestrator.db import TaskStore
from agent_orchestrator.models import Priority, Risk


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentctl")
    parser.add_argument("--runtime-root", default=None, help="override runtime root")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="add a task")
    add_parser.add_argument("--repo", required=True)
    add_parser.add_argument("--task", required=True)
    add_parser.add_argument("--risk", choices=[risk.value for risk in Risk], default=Risk.NORMAL.value)
    add_parser.add_argument("--priority", choices=[priority.value for priority in Priority], default=Priority.NORMAL.value)

    subparsers.add_parser("list", help="list tasks")

    show_parser = subparsers.add_parser("show", help="show task details")
    show_parser.add_argument("task_id")

    daemon_parser = subparsers.add_parser("daemon", help="run daemon")
    daemon_parser.add_argument("--once", action="store_true")
    daemon_parser.add_argument("--idle-sleep", type=float, default=2.0)
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
        )
        print(f"added {task.id}")
        print(f"status: {task.status.value}")
        print(f"repo: {task.repo_path}")
        return

    if args.command == "list":
        print("task_id\tstatus\trepo\trisk\tpriority\tcreated_at\tupdated_at\tshort_task")
        for task in store.list_tasks():
            short = task.task.replace("\n", " ")[:60]
            print(
                f"{task.id}\t{task.status.value}\t{task.repo_path}\t{task.risk.value}\t"
                f"{task.priority.value}\t{task.created_at.isoformat()}\t"
                f"{task.updated_at.isoformat()}\t{short}"
            )
        return

    if args.command == "show":
        task = store.get_task(args.task_id)
        if task is None:
            raise SystemExit(f"task not found: {args.task_id}")
        log_path = config.logs_dir / task.id
        diff_path = log_path / "diff.patch"
        print(f"task_id: {task.id}")
        print(f"task: {task.task}")
        print(f"status: {task.status.value}")
        print(f"repo: {task.repo_path}")
        print(f"workspace_path: {task.workspace_path}")
        print(f"branch_name: {task.branch_name}")
        print(f"log_path: {log_path}")
        print(f"result_summary: {task.result_summary}")
        print(f"diff_path: {diff_path}")
        if task.error:
            print(f"error: {task.error}")
        return

    if args.command == "daemon":
        import asyncio

        asyncio.run(run_daemon(store, once=args.once, idle_sleep=args.idle_sleep))
