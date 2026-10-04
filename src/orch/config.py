from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


RUNTIME_ROOT_ENV = "AGENT_ORCHESTRATOR_RUNTIME_ROOT"
ROUTING_PATH_ENV = "AGENT_ORCHESTRATOR_ROUTING"
DEFAULT_ROUTING_PATH = "~/.config/agent-orchestrator/routing.toml"


def default_routing_path() -> Path:
    raw = os.environ.get(ROUTING_PATH_ENV) or DEFAULT_ROUTING_PATH
    return Path(raw).expanduser()


@dataclass(frozen=True, slots=True)
class Config:
    runtime_root: Path
    #: Optional TOML file that overrides the built-in routing table.
    routing_path: Path = field(default_factory=default_routing_path)

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


def load_config(runtime_root: str | None = None, routing_path: str | None = None) -> Config:
    root = runtime_root or os.environ.get(RUNTIME_ROOT_ENV) or "~/agent-runtime"
    routing = Path(routing_path).expanduser() if routing_path else default_routing_path()
    return Config(runtime_root=Path(root).expanduser().resolve(), routing_path=routing)


def ensure_runtime_dirs(config: Config) -> None:
    config.runtime_root.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    config.workspaces_dir.mkdir(parents=True, exist_ok=True)
    config.sessions_dir.mkdir(parents=True, exist_ok=True)
    config.repos_dir.mkdir(parents=True, exist_ok=True)
