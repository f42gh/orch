from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from agent_orchestrator.config import Config, load_config
from agent_orchestrator.daemon import process_one
from agent_orchestrator.db import TaskStore
from agent_orchestrator.models import Priority, Risk, Task


LogKind = Literal["agent", "stdout", "stderr"]


class CreateTaskRequest(BaseModel):
    repo: str = Field(min_length=1)
    task: str = Field(min_length=1)
    risk: Risk = Risk.NORMAL
    priority: Priority = Priority.NORMAL


def serialize_task(config: Config, task: Task) -> dict[str, object]:
    log_path = config.logs_dir / task.id
    return {
        "id": task.id,
        "repo_path": str(task.repo_path),
        "workspace_path": str(task.workspace_path) if task.workspace_path else None,
        "branch_name": task.branch_name,
        "session_id": task.session_id,
        "task": task.task,
        "risk": task.risk.value,
        "priority": task.priority.value,
        "status": task.status.value,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "result_summary": task.result_summary,
        "error": task.error,
        "log_path": str(log_path),
        "diff_path": str(log_path / "diff.patch"),
        "result_path": str(log_path / "result.json"),
    }


def missing_artifact(path: Path) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"message": "not available yet", "path": str(path)},
    )


def read_text_artifact(path: Path) -> dict[str, object]:
    if not path.exists():
        raise missing_artifact(path)
    return {
        "path": str(path),
        "content": path.read_text(encoding="utf-8"),
    }


def create_app(config: Config | None = None) -> FastAPI:
    app_config = config or load_config()
    store = TaskStore(app_config)
    app = FastAPI(title="Agent Orchestrator API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "runtime_root": str(app_config.runtime_root),
        }

    @app.get("/tasks")
    def list_tasks() -> dict[str, object]:
        return {"tasks": [serialize_task(app_config, task) for task in store.list_tasks()]}

    @app.post("/tasks", status_code=201)
    def create_task(request: CreateTaskRequest) -> dict[str, object]:
        task = store.add_task(
            repo_path=Path(request.repo),
            task=request.task,
            risk=request.risk,
            priority=request.priority,
        )
        return {"task": serialize_task(app_config, task)}

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, object]:
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail={"message": "task not found"})
        return {"task": serialize_task(app_config, task)}

    @app.post("/daemon/process-one")
    async def process_one_task() -> dict[str, object]:
        processed = await process_one(store)
        return {"processed": processed}

    @app.get("/tasks/{task_id}/logs/{kind}")
    def get_log(task_id: str, kind: LogKind) -> dict[str, object]:
        if store.get_task(task_id) is None:
            raise HTTPException(status_code=404, detail={"message": "task not found"})
        return read_text_artifact(app_config.logs_dir / task_id / f"{kind}.log")

    @app.get("/tasks/{task_id}/diff")
    def get_diff(task_id: str) -> dict[str, object]:
        if store.get_task(task_id) is None:
            raise HTTPException(status_code=404, detail={"message": "task not found"})
        return read_text_artifact(app_config.logs_dir / task_id / "diff.patch")

    @app.get("/tasks/{task_id}/result")
    def get_result(task_id: str) -> dict[str, object]:
        if store.get_task(task_id) is None:
            raise HTTPException(status_code=404, detail={"message": "task not found"})
        path = app_config.logs_dir / task_id / "result.json"
        if not path.exists():
            raise missing_artifact(path)
        return {
            "path": str(path),
            "result": json.loads(path.read_text(encoding="utf-8")),
        }

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentapi")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="run local API server")
    run_parser.add_argument("--host", default="127.0.0.1")
    run_parser.add_argument("--port", type=int, default=8765)
    run_parser.add_argument("--runtime-root", default=None)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "run":
        import uvicorn

        config = load_config(args.runtime_root)
        uvicorn.run(create_app(config), host=args.host, port=args.port)
