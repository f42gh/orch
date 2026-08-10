from pathlib import Path

from agent_orchestrator.config import Config
from agent_orchestrator.db import TaskStore
from agent_orchestrator.models import Priority, Risk, TaskStatus


def test_add_update_and_list_task(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))

    task = store.add_task(tmp_path, "READMEを更新して", Risk.NORMAL, Priority.HIGH)

    assert task.id == "task-0001"
    assert task.status == TaskStatus.QUEUED
    assert task.risk == Risk.NORMAL

    store.set_status(task.id, TaskStatus.RUNNING)
    updated = store.get_task(task.id)
    assert updated is not None
    assert updated.status == TaskStatus.RUNNING

    tasks = store.list_tasks()
    assert [item.id for item in tasks] == ["task-0001"]


def test_next_queued_task_uses_priority(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))

    low = store.add_task(tmp_path, "low", Risk.NORMAL, Priority.LOW)
    high = store.add_task(tmp_path, "high", Risk.NORMAL, Priority.HIGH)

    next_task = store.next_queued_task()
    assert next_task is not None
    assert next_task.id == high.id
    assert low.id == "task-0001"
