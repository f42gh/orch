from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_orchestrator.config import Config


def task_log_dir(config: Config, task_id: str) -> Path:
    path = config.logs_dir / task_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
