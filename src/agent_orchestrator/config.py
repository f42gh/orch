from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


RUNTIME_ROOT_ENV = "AGENT_ORCHESTRATOR_RUNTIME_ROOT"


@dataclass(frozen=True, slots=True)
class Config:
    runtime_root: Path

    @property
    def db_path(self) -> Path:
        return self.runtime_root / "tasks.db"

    @property
    def logs_dir(self) -> Path:
        return self.runtime_root / "logs"

    @property
    def workspaces_dir(self) -> Path:
        return self.runtime_root / "workspaces"

    @property
    def sessions_dir(self) -> Path:
        return self.runtime_root / "sessions"

    @property
    def repos_dir(self) -> Path:
        return self.runtime_root / "repos"


def load_config(runtime_root: str | None = None) -> Config:
    root = runtime_root or os.environ.get(RUNTIME_ROOT_ENV) or "~/agent-runtime"
    return Config(runtime_root=Path(root).expanduser().resolve())


def ensure_runtime_dirs(config: Config) -> None:
    config.runtime_root.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    config.workspaces_dir.mkdir(parents=True, exist_ok=True)
    config.sessions_dir.mkdir(parents=True, exist_ok=True)
    config.repos_dir.mkdir(parents=True, exist_ok=True)
