from __future__ import annotations

import argparse
import asyncio
import time

from agent_orchestrator.config import load_config
from agent_orchestrator.db import TaskStore
from agent_orchestrator.models import TaskStatus
from agent_orchestrator.workspace import WorkspaceError, create_workspace
from agent_orchestrator.worker_claude import run_claude_task


async def process_one(store: TaskStore) -> bool:
    task = store.next_queued_task()
    if task is None:
        return False

    store.set_status(task.id, TaskStatus.RUNNING)
    try:
        workspace_path, branch_name = create_workspace(store.config, task.id, task.repo_path)
        store.update_task(task.id, workspace_path=workspace_path, branch_name=branch_name)
        task = store.get_task(task.id)
        if task is None:
            raise RuntimeError("task disappeared after workspace creation")
        await run_claude_task(store.config, store, task)
    except WorkspaceError as exc:
        store.set_status(task.id, TaskStatus.FAILED, str(exc))
    except Exception as exc:
        store.set_status(task.id, TaskStatus.FAILED, str(exc))
    return True


async def run_daemon(store: TaskStore, once: bool, idle_sleep: float) -> None:
    while True:
        processed = await process_one(store)
        if once:
            return
        if not processed:
            time.sleep(idle_sleep)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentd")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="run daemon loop")
    run_parser.add_argument("--runtime-root", default=None)
    run_parser.add_argument("--once", action="store_true", help="process at most one queued task")
    run_parser.add_argument("--idle-sleep", type=float, default=2.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "run":
        config = load_config(args.runtime_root)
        store = TaskStore(config)
        asyncio.run(run_daemon(store, once=args.once, idle_sleep=args.idle_sleep))
