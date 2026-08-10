from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from agent_orchestrator.config import Config, ensure_runtime_dirs
from agent_orchestrator.models import (
    Engine,
    Priority,
    Risk,
    Task,
    TaskKind,
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

#: Columns added after v0. Applied with ALTER TABLE so existing databases survive.
ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("kind", "TEXT NOT NULL DEFAULT 'implement'"),
    ("engine", "TEXT"),
    ("engine_session_id", "TEXT"),
    ("parent_id", "TEXT"),
    ("base_ref", "TEXT"),
    ("cost_usd", "REAL"),
    ("exit_code", "INTEGER"),
)

#: Ordering used whenever the queue is drained. Cheaper than sorting in Python.
PRIORITY_ORDER_SQL = "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, created_at"


class TaskStore:
    def __init__(self, config: Config):
        self.config = config
        ensure_runtime_dirs(config)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.config.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL lets the MCP server, the daemon, the API and several workers touch the
        # same file concurrently instead of tripping over "database is locked".
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        """Open a write transaction that takes the reserved lock up front."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> list[str]:
        """Add columns introduced after v0. Returns the columns that were added."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        added: list[str] = []
        for column, definition in ADDED_COLUMNS:
            if column in existing:
                continue
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
            added.append(column)
        return added

    def next_task_id(self) -> str:
        with self._conn() as conn:
            return _next_task_id(conn)

    def add_task(
        self,
        repo_path: Path,
        task: str,
        risk: Risk = Risk.NORMAL,
        priority: Priority = Priority.NORMAL,
        kind: TaskKind = TaskKind.IMPLEMENT,
        engine: Engine | None = None,
        parent_id: str | None = None,
        base_ref: str | None = None,
    ) -> Task:
        now = utc_now_iso()
        session_id = str(uuid.uuid4())
        # The id is derived from existing rows, so allocate it inside the write
        # transaction or two concurrent dispatches race for the same number.
        with self._immediate() as conn:
            task_id = _next_task_id(conn)
            conn.execute(
                """
                INSERT INTO tasks (
                    id, repo_path, task, risk, priority, status,
                    created_at, updated_at, session_id, kind, engine, parent_id, base_ref
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    kind.value,
                    engine.value if engine else None,
                    parent_id,
                    base_ref,
                ),
            )
        created = self.get_task(task_id)
        if created is None:
            raise RuntimeError(f"failed to create task {task_id}")
        return created

    def get_task(self, task_id: str) -> Task | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row_to_task(row) if row else None

    def list_tasks(
        self,
        status: TaskStatus | None = None,
        parent_id: str | None = None,
    ) -> list[Task]:
        clauses: list[str] = []
        values: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            values.append(status.value)
        if parent_id is not None:
            clauses.append("parent_id = ?")
            values.append(parent_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM tasks{where} ORDER BY created_at ASC", values
            ).fetchall()
        return [row_to_task(row) for row in rows]

    def next_queued_task(self) -> Task | None:
        """Peek at the head of the queue without claiming it."""
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT * FROM tasks WHERE status = ? ORDER BY {PRIORITY_ORDER_SQL} LIMIT 1",
                (TaskStatus.QUEUED.value,),
            ).fetchone()
        return row_to_task(row) if row else None

    def claim_next_task(self) -> Task | None:
        """Atomically move the head of the queue to running and return it.

        Selecting and updating in one write transaction is what keeps two workers
        from picking up the same task.
        """
        with self._immediate() as conn:
            row = conn.execute(
                f"SELECT id FROM tasks WHERE status = ? ORDER BY {PRIORITY_ORDER_SQL} LIMIT 1",
                (TaskStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                return None
            claimed = _claim(conn, str(row["id"]))
        return self.get_task(claimed) if claimed else None

    def claim_task(self, task_id: str) -> Task | None:
        """Claim one specific queued task. Returns None if someone else got there first."""
        with self._immediate() as conn:
            claimed = _claim(conn, task_id)
        return self.get_task(claimed) if claimed else None

    def update_task(self, task_id: str, **fields: object) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{field} = ?" for field in fields)
        values = [normalize_value(value) for value in fields.values()]
        values.append(task_id)
        with self._conn() as conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", values)

    def set_status(self, task_id: str, status: TaskStatus, error: str | None = None) -> None:
        self.update_task(task_id, status=status, error=error)


def _next_task_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT id FROM tasks WHERE id LIKE 'task-%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "task-0001"
    try:
        number = int(str(row["id"]).split("-")[-1]) + 1
    except ValueError:
        number = 1
    return f"task-{number:04d}"


def _claim(conn: sqlite3.Connection, task_id: str) -> str | None:
    cursor = conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
        (TaskStatus.RUNNING.value, utc_now_iso(), task_id, TaskStatus.QUEUED.value),
    )
    return task_id if cursor.rowcount == 1 else None


def normalize_value(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (Risk, Priority, TaskStatus, Engine, TaskKind)):
        return value.value
    return value


def row_to_task(row: sqlite3.Row) -> Task:
    keys = row.keys()

    def optional(name: str) -> object | None:
        return row[name] if name in keys else None

    engine = optional("engine")
    kind = optional("kind")
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
        kind=TaskKind(kind) if kind else TaskKind.IMPLEMENT,
        engine=Engine(engine) if engine else None,
        engine_session_id=optional("engine_session_id"),
        parent_id=optional("parent_id"),
        base_ref=optional("base_ref"),
        cost_usd=optional("cost_usd"),
        exit_code=optional("exit_code"),
        result_summary=row["result_summary"],
        error=row["error"],
    )
