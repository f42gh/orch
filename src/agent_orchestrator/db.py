from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from agent_orchestrator.config import Config, ensure_runtime_dirs
from agent_orchestrator.models import (
    Engine,
    FallbackMode,
    PreparedWorkflowTask,
    Priority,
    Risk,
    Task,
    TaskKind,
    TaskStatus,
    Workflow,
    WorkflowDetails,
    WorkflowRoute,
    WorkflowStatus,
    WorkflowTask,
    WorkflowType,
    parse_datetime,
    utc_now_iso,
)
from agent_orchestrator.workspace import branch_name_for_task


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

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    workflow_type TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_workflows_type_created
ON workflows(workflow_type, created_at);

CREATE TABLE IF NOT EXISTS workflow_routes (
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    primary_engine TEXT NOT NULL,
    fallback_mode TEXT NOT NULL,
    fallback_engines TEXT NOT NULL,
    PRIMARY KEY (workflow_id, kind)
);

CREATE TABLE IF NOT EXISTS workflow_tasks (
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (workflow_id, ordinal),
    UNIQUE (workflow_id, task_id)
);
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
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
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
            _ensure_wal(conn)
            conn.executescript(SCHEMA)
        # Several CLI/MCP processes can discover the same legacy database at once.
        # Take the write lock before reading table_info so only one process decides a
        # column is missing; the next waiter then observes the completed migration.
        with self._immediate() as conn:
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
        # The id is derived from existing rows, so allocate it inside the write
        # transaction or two concurrent dispatches race for the same number.
        with self._immediate() as conn:
            task_id = _insert_task(
                conn,
                repo_path=repo_path,
                task=task,
                risk=risk,
                priority=priority,
                kind=kind,
                engine=engine,
                parent_id=parent_id,
                base_ref=base_ref,
                now=now,
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

    def create_run(self, repo_path: Path, routes: tuple[WorkflowRoute, ...]) -> Workflow:
        """Persist an open run and its complete route snapshot atomically."""
        _validate_route_snapshot(routes)
        now = utc_now_iso()
        with self._immediate() as conn:
            workflow_id = _next_workflow_id(conn, WorkflowType.RUN)
            _insert_workflow(
                conn,
                workflow_id=workflow_id,
                workflow_type=WorkflowType.RUN,
                repo_path=repo_path,
                status=WorkflowStatus.OPEN,
                now=now,
            )
            _insert_workflow_routes(conn, workflow_id, routes)
        created = self.get_workflow(workflow_id)
        if created is None:
            raise RuntimeError(f"failed to create workflow {workflow_id}")
        return created

    def append_workflow_task(
        self,
        workflow_id: str,
        prepared: PreparedWorkflowTask,
    ) -> tuple[Task, WorkflowTask]:
        """Append one task to an open run with a transactionally assigned ordinal."""
        with self._immediate() as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"workflow not found: {workflow_id}")
            workflow = row_to_workflow(row)
            if workflow.workflow_type is not WorkflowType.RUN:
                raise ValueError(f"workflow {workflow_id} is a sealed batch")
            if workflow.status is not WorkflowStatus.OPEN:
                raise ValueError(f"workflow {workflow_id} is closed")

            ordinal = int(
                conn.execute(
                    """
                    SELECT COALESCE(MAX(ordinal) + 1, 0)
                    FROM workflow_tasks WHERE workflow_id = ?
                    """,
                    (workflow_id,),
                ).fetchone()[0]
            )
            task_id = _insert_prepared_task(
                conn, workflow.repo_path, prepared, utc_now_iso()
            )
            conn.execute(
                """
                INSERT INTO workflow_tasks (workflow_id, task_id, ordinal)
                VALUES (?, ?, ?)
                """,
                (workflow_id, task_id, ordinal),
            )

        task = self.get_task(task_id)
        if task is None:
            raise RuntimeError(f"failed to create task {task_id}")
        return task, WorkflowTask(workflow_id, task_id, ordinal)

    def create_batch(
        self,
        repo_path: Path,
        routes: tuple[WorkflowRoute, ...],
        prepared_tasks: tuple[PreparedWorkflowTask, ...],
    ) -> WorkflowDetails:
        """Insert a sealed batch, all tasks, and all memberships in one transaction."""
        _validate_route_snapshot(routes)
        if not prepared_tasks:
            raise ValueError("a batch must contain at least one task")

        now = utc_now_iso()
        with self._immediate() as conn:
            workflow_id = _next_workflow_id(conn, WorkflowType.BATCH)
            _insert_workflow(
                conn,
                workflow_id=workflow_id,
                workflow_type=WorkflowType.BATCH,
                repo_path=repo_path,
                status=WorkflowStatus.SEALED,
                now=now,
            )
            _insert_workflow_routes(conn, workflow_id, routes)
            for ordinal, prepared in enumerate(prepared_tasks):
                task_id = _insert_prepared_task(conn, repo_path, prepared, now)
                conn.execute(
                    """
                    INSERT INTO workflow_tasks (workflow_id, task_id, ordinal)
                    VALUES (?, ?, ?)
                    """,
                    (workflow_id, task_id, ordinal),
                )

        details = self.get_workflow_details(workflow_id)
        if details is None:
            raise RuntimeError(f"failed to create workflow {workflow_id}")
        return details

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
        return row_to_workflow(row) if row else None

    def list_workflows(
        self,
        workflow_type: WorkflowType | None = None,
    ) -> list[Workflow]:
        where = " WHERE workflow_type = ?" if workflow_type is not None else ""
        values = (workflow_type.value,) if workflow_type is not None else ()
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM workflows{where} ORDER BY created_at, id", values
            ).fetchall()
        return [row_to_workflow(row) for row in rows]

    def get_workflow_routes(self, workflow_id: str) -> tuple[WorkflowRoute, ...]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_routes WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchall()
        by_kind = {TaskKind(row["kind"]): row_to_workflow_route(row) for row in rows}
        return tuple(by_kind[kind] for kind in TaskKind if kind in by_kind)

    def get_workflow_memberships(self, workflow_id: str) -> tuple[WorkflowTask, ...]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT workflow_id, task_id, ordinal FROM workflow_tasks
                WHERE workflow_id = ? ORDER BY ordinal
                """,
                (workflow_id,),
            ).fetchall()
        return tuple(
            WorkflowTask(row["workflow_id"], row["task_id"], row["ordinal"])
            for row in rows
        )

    def get_workflow_tasks(self, workflow_id: str) -> tuple[Task, ...]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT tasks.* FROM workflow_tasks
                JOIN tasks ON tasks.id = workflow_tasks.task_id
                WHERE workflow_tasks.workflow_id = ?
                ORDER BY workflow_tasks.ordinal
                """,
                (workflow_id,),
            ).fetchall()
        return tuple(row_to_task(row) for row in rows)

    def get_workflow_details(self, workflow_id: str) -> WorkflowDetails | None:
        # A show response should describe one database instant even while other
        # dispatchers append to the run between reads.
        with self._conn() as conn:
            conn.execute("BEGIN")
            try:
                workflow_row = conn.execute(
                    "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
                ).fetchone()
                if workflow_row is None:
                    conn.execute("COMMIT")
                    return None
                route_rows = conn.execute(
                    "SELECT * FROM workflow_routes WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchall()
                membership_rows = conn.execute(
                    """
                    SELECT workflow_id, task_id, ordinal FROM workflow_tasks
                    WHERE workflow_id = ? ORDER BY ordinal
                    """,
                    (workflow_id,),
                ).fetchall()
                task_rows = conn.execute(
                    """
                    SELECT tasks.* FROM workflow_tasks
                    JOIN tasks ON tasks.id = workflow_tasks.task_id
                    WHERE workflow_tasks.workflow_id = ?
                    ORDER BY workflow_tasks.ordinal
                    """,
                    (workflow_id,),
                ).fetchall()
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

        routes_by_kind = {
            TaskKind(row["kind"]): row_to_workflow_route(row) for row in route_rows
        }
        return WorkflowDetails(
            workflow=row_to_workflow(workflow_row),
            routes=tuple(
                routes_by_kind[kind] for kind in TaskKind if kind in routes_by_kind
            ),
            memberships=tuple(
                WorkflowTask(row["workflow_id"], row["task_id"], row["ordinal"])
                for row in membership_rows
            ),
            tasks=tuple(row_to_task(row) for row in task_rows),
        )

    def close_workflow(self, workflow_id: str) -> Workflow:
        """Close a run. Closing an already closed run is idempotent."""
        with self._immediate() as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"workflow not found: {workflow_id}")
            workflow = row_to_workflow(row)
            if workflow.workflow_type is not WorkflowType.RUN:
                raise ValueError(f"workflow {workflow_id} is a sealed batch")
            if workflow.status is WorkflowStatus.OPEN:
                conn.execute(
                    """
                    UPDATE workflows SET status = ?, closed_at = ? WHERE id = ?
                    """,
                    (WorkflowStatus.CLOSED.value, utc_now_iso(), workflow_id),
                )
        closed = self.get_workflow(workflow_id)
        if closed is None:
            raise RuntimeError(f"workflow disappeared while closing: {workflow_id}")
        return closed


def _validate_route_snapshot(routes: tuple[WorkflowRoute, ...]) -> None:
    kinds = [route.kind for route in routes]
    if len(kinds) != len(TaskKind) or set(kinds) != set(TaskKind):
        raise ValueError(
            "workflow routes must contain exactly one route for every task kind"
        )


def _ensure_wal(conn: sqlite3.Connection) -> None:
    """Enable persistent WAL mode once, tolerating concurrent first-time openers."""
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(20):
        try:
            current = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if current == "wal":
                return
            selected = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            if selected != "wal":
                raise RuntimeError(f"SQLite refused WAL journal mode (selected {selected})")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            last_error = exc
            time.sleep(min(0.01 * (2**attempt), 0.25))
    assert last_error is not None
    raise last_error


def _insert_workflow(
    conn: sqlite3.Connection,
    *,
    workflow_id: str,
    workflow_type: WorkflowType,
    repo_path: Path,
    status: WorkflowStatus,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO workflows (
            id, workflow_type, repo_path, status, created_at, closed_at
        ) VALUES (?, ?, ?, ?, ?, NULL)
        """,
        (
            workflow_id,
            workflow_type.value,
            str(repo_path.expanduser().resolve()),
            status.value,
            now,
        ),
    )


def _insert_workflow_routes(
    conn: sqlite3.Connection,
    workflow_id: str,
    routes: tuple[WorkflowRoute, ...],
) -> None:
    conn.executemany(
        """
        INSERT INTO workflow_routes (
            workflow_id, kind, primary_engine, fallback_mode, fallback_engines
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                workflow_id,
                route.kind.value,
                route.primary.value,
                route.fallback_mode.value,
                json.dumps([engine.value for engine in route.fallbacks]),
            )
            for route in routes
        ],
    )


def _insert_prepared_task(
    conn: sqlite3.Connection,
    repo_path: Path,
    prepared: PreparedWorkflowTask,
    now: str,
) -> str:
    request = prepared.request
    return _insert_task(
        conn,
        repo_path=repo_path,
        task=request.task,
        risk=request.risk,
        priority=request.priority,
        kind=request.kind,
        engine=prepared.engine,
        parent_id=request.parent_id,
        base_ref=request.base_ref,
        assign_branch=True,
        now=now,
    )


def _insert_task(
    conn: sqlite3.Connection,
    *,
    repo_path: Path,
    task: str,
    risk: Risk,
    priority: Priority,
    kind: TaskKind,
    engine: Engine | None,
    parent_id: str | None,
    base_ref: str | None,
    now: str,
    branch_name: str | None = None,
    assign_branch: bool = False,
) -> str:
    task_id = _next_task_id(conn)
    if assign_branch and engine is not None:
        branch_name = branch_name_for_task(task_id, engine)
    conn.execute(
        """
        INSERT INTO tasks (
            id, repo_path, task, risk, priority, status,
            created_at, updated_at, session_id, kind, engine, parent_id,
            base_ref, branch_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            str(uuid.uuid4()),
            kind.value,
            engine.value if engine else None,
            parent_id,
            base_ref,
            branch_name,
        ),
    )
    return task_id


def _next_task_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """
        SELECT MAX(CAST(substr(id, 6) AS INTEGER)) AS number
        FROM tasks
        WHERE id GLOB 'task-[0-9]*'
          AND substr(id, 6) NOT GLOB '*[^0-9]*'
        """
    ).fetchone()
    number = int(row["number"] or 0) + 1
    return f"task-{number:04d}"


def _next_workflow_id(
    conn: sqlite3.Connection,
    workflow_type: WorkflowType,
) -> str:
    prefix = workflow_type.value
    row = conn.execute(
        """
        SELECT MAX(CAST(substr(id, ?) AS INTEGER)) AS number
        FROM workflows WHERE id GLOB ?
        """,
        (len(prefix) + 2, f"{prefix}-[0-9]*"),
    ).fetchone()
    number = int(row["number"] or 0) + 1
    return f"{prefix}-{number:04d}"


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


def row_to_workflow(row: sqlite3.Row) -> Workflow:
    return Workflow(
        id=row["id"],
        workflow_type=WorkflowType(row["workflow_type"]),
        repo_path=Path(row["repo_path"]),
        status=WorkflowStatus(row["status"]),
        created_at=parse_datetime(row["created_at"]),
        closed_at=parse_datetime(row["closed_at"]) if row["closed_at"] else None,
    )


def row_to_workflow_route(row: sqlite3.Row) -> WorkflowRoute:
    raw_fallbacks = json.loads(row["fallback_engines"])
    if not isinstance(raw_fallbacks, list):
        raise ValueError("persisted workflow fallbacks must be a list")
    return WorkflowRoute(
        kind=TaskKind(row["kind"]),
        primary=Engine(row["primary_engine"]),
        fallback_mode=FallbackMode(row["fallback_mode"]),
        fallbacks=tuple(Engine(value) for value in raw_fallbacks),
    )
