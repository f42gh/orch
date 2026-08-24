from __future__ import annotations

import argparse
import asyncio

from agent_orchestrator.config import Config, load_config
from agent_orchestrator.db import TaskStore
from agent_orchestrator.engines import available_engines
from agent_orchestrator.models import Task, TaskStatus
from agent_orchestrator.router import RoutingError, load_routing_table, resolve_engine
from agent_orchestrator.workspace import WorkspaceError, create_workspace
from agent_orchestrator.worker import run_task

DEFAULT_CONCURRENCY = 2


def _prepare(store: TaskStore, task: Task) -> Task:
    """Assign an engine and a worktree to a claimed task."""
    if task.engine is None:
        table = load_routing_table(store.config.routing_path)
        engine = resolve_engine(task.kind, available_engines(), table=table)
        store.update_task(task.id, engine=engine)
        task = _reload(store, task.id)

    workspace_path, branch_name = create_workspace(
        store.config, task.id, task.repo_path, engine=task.engine, base_ref=task.base_ref
    )
    store.update_task(task.id, workspace_path=workspace_path, branch_name=branch_name)
    return _reload(store, task.id)


def _reload(store: TaskStore, task_id: str) -> Task:
    task = store.get_task(task_id)
    if task is None:
        raise RuntimeError(f"task {task_id} disappeared mid-flight")
    return task


async def _execute(store: TaskStore, task: Task) -> None:
    """Run one already-claimed task, recording any failure against it."""
    try:
        prepared = _prepare(store, task)
    except (WorkspaceError, RoutingError) as exc:
        store.set_status(task.id, TaskStatus.FAILED, str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - a stuck "running" row helps nobody
        store.set_status(task.id, TaskStatus.FAILED, str(exc))
        return

    # run_task blocks on a subprocess, so keep the event loop free for siblings.
    try:
        await asyncio.to_thread(run_task, store.config, store, prepared)
    except Exception as exc:  # noqa: BLE001
        store.set_status(task.id, TaskStatus.FAILED, str(exc))


async def process_one(store: TaskStore) -> bool:
    """Claim and run at most one queued task. Returns False when the queue is empty."""
    task = store.claim_next_task()
    if task is None:
        return False
    await _execute(store, task)
    return True


async def run_one_task(store: TaskStore, task_id: str) -> TaskStatus:
    """Run a specific queued task. This is the entry point the MCP server spawns."""
    task = store.claim_task(task_id)
    if task is None:
        current = store.get_task(task_id)
        if current is None:
            raise RuntimeError(f"task not found: {task_id}")
        raise RuntimeError(f"task {task_id} is {current.status.value}, not queued")
    await _execute(store, task)
    return _reload(store, task_id).status


async def run_daemon(
    store: TaskStore,
    once: bool,
    idle_sleep: float,
    max_concurrency: int = DEFAULT_CONCURRENCY,
) -> None:
    """Drain the queue, running up to `max_concurrency` tasks at a time."""
    if once:
        await process_one(store)
        return

    running: set[asyncio.Task[None]] = set()
    while True:
        while len(running) < max_concurrency:
            task = store.claim_next_task()
            if task is None:
                break
            job = asyncio.create_task(_execute(store, task))
            running.add(job)
            job.add_done_callback(running.discard)

        if running:
            await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
        else:
            await asyncio.sleep(idle_sleep)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Populate a parser with the daemon's own commands.

    Mounted twice: as `orch daemon` and as the whole of `agentd`. Defined once so the
    two cannot drift — an earlier `orch daemon` was a hand-copied second version of
    this and had already lost track of DEFAULT_CONCURRENCY.
    """
    subparsers = parser.add_subparsers(dest="daemon_command", required=True)

    run_parser = subparsers.add_parser("run", help="claim and run queued tasks until stopped")
    run_parser.add_argument(
        "--runtime-root", default=argparse.SUPPRESS, metavar="PATH",
        help="override the runtime root",
    )
    run_parser.add_argument("--once", action="store_true", help="process at most one queued task")
    run_parser.add_argument(
        "--idle-sleep", type=float, default=2.0, metavar="SECONDS",
        help="how long to wait before looking for work again (default: 2.0)",
    )
    run_parser.add_argument(
        "--max-concurrency", type=int, default=DEFAULT_CONCURRENCY, metavar="N",
        help=f"how many tasks may run at once (default: {DEFAULT_CONCURRENCY})",
    )

    task_parser = subparsers.add_parser("run-task", help="run one specific queued task")
    task_parser.add_argument("task_id", metavar="TASK_ID", help="the queued task to run")
    # SUPPRESS, not None: mounted under `orch` these sit inside a parser that has
    # already set runtime_root, and an argparse subparser default overwrites it.
    task_parser.add_argument(
        "--runtime-root", default=argparse.SUPPRESS, metavar="PATH",
        help="override the runtime root",
    )


def run(args: argparse.Namespace) -> None:
    """Execute a parsed daemon command. Shared by `orch daemon` and `agentd`."""
    config: Config = load_config(getattr(args, "runtime_root", None))
    store = TaskStore(config)

    if args.daemon_command == "run":
        try:
            asyncio.run(
                run_daemon(
                    store,
                    once=args.once,
                    idle_sleep=args.idle_sleep,
                    max_concurrency=args.max_concurrency,
                )
            )
        except KeyboardInterrupt:
            # Ctrl-C is how this loop is meant to end. Running tasks are detached
            # workers and keep going; the daemon simply stops claiming new ones.
            raise SystemExit(130) from None
        return

    status = asyncio.run(run_one_task(store, args.task_id))
    print(f"{args.task_id}: {status.value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentd", description="Deprecated alias for `orch daemon`."
    )
    add_arguments(parser)
    return parser


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
