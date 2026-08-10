from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Iterable

from agent_orchestrator.config import Config, ensure_runtime_dirs
from agent_orchestrator.models import (
    Priority,
    Risk,
    Task,
    TaskStatus,
    parse_datetime,
    utc_now_iso,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    repo_path TEXT NOT NULL,
    workspace_path TEXT,
    branch_name TEXT,
    session_id TEXT,
    task TEXT NOT NULL,
    risk TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    result_summary TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_priority_created
ON tasks(status, priority, created_at);
"""


class TaskStore:
    def __init__(self, config: Config):
        self.config = config
        ensure_runtime_dirs(config)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.config.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def next_task_id(self) -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM tasks ORDER BY created_at DESC LIMIT 1").fetchone()
        if row is None:
            return "task-0001"
        try:
            number = int(str(row["id"]).split("-")[-1]) + 1
        except ValueError:
            number = 1
        return f"task-{number:04d}"

    def add_task(
        self,
        repo_path: Path,
        task: str,
        risk: Risk = Risk.NORMAL,
        priority: Priority = Priority.NORMAL,
    ) -> Task:
        now = utc_now_iso()
        task_id = self.next_task_id()
        session_id = str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, repo_path, task, risk, priority, status,
                    created_at, updated_at, session_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    str(repo_path.expanduser().resolve()),
                    task,
                    risk.value,
                    priority.value,
                    TaskStatus.QUEUED.value,
                    now,
                    now,
                    session_id,
                ),
            )
        created = self.get_task(task_id)
        if created is None:
            raise RuntimeError(f"failed to create task {task_id}")
        return created

    def get_task(self, task_id: str) -> Task | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row_to_task(row) if row else None

    def list_tasks(self) -> list[Task]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at ASC").fetchall()
        return [row_to_task(row) for row in rows]

    def next_queued_task(self) -> Task | None:
        priority_order = {
            Priority.HIGH.value: 0,
            Priority.NORMAL.value: 1,
            Priority.LOW.value: 2,
        }
        queued = [task for task in self.list_tasks() if task.status == TaskStatus.QUEUED]
        queued.sort(key=lambda task: (priority_order[task.priority.value], task.created_at))
        return queued[0] if queued else None

    def update_task(self, task_id: str, **fields: object) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{field} = ?" for field in fields)
        values = [normalize_value(value) for value in fields.values()]
        values.append(task_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", values)

    def set_status(self, task_id: str, status: TaskStatus, error: str | None = None) -> None:
        self.update_task(task_id, status=status, error=error)


def normalize_value(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (Risk, Priority, TaskStatus)):
        return value.value
    return value


def row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        repo_path=Path(row["repo_path"]),
        workspace_path=Path(row["workspace_path"]) if row["workspace_path"] else None,
        branch_name=row["branch_name"],
        session_id=row["session_id"],
        task=row["task"],
        risk=Risk(row["risk"]),
        priority=Priority(row["priority"]),
        status=TaskStatus(row["status"]),
        created_at=parse_datetime(row["created_at"]),
        updated_at=parse_datetime(row["updated_at"]),
        result_summary=row["result_summary"],
        error=row["error"],
    )
