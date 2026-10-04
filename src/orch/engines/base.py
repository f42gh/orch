"""Common shape for every engine adapter.

An adapter is deliberately pure: it builds an argv and parses bytes. Spawning,
logging, timeouts and database writes all live in `worker`, so adapters can be
tested against the recorded fixtures in `tests/fixtures/` without running anything.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from orch.models import Engine, Task, TokenUsage
from orch.router import EnginePolicy


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What the *installed* binary can do, discovered by probing it."""

    engine: Engine
    path: str
    version: str
    #: True when the binary can emit machine-readable output. agy 1.0.12 cannot.
    structured_output: bool
    #: True when the binary reports what a run cost. codex and agy do not.
    reports_cost: bool
    notes: tuple[str, ...] = ()

    def describe(self) -> dict[str, object]:
        return {
            "engine": self.engine.value,
            "path": self.path,
            "version": self.version,
            "structured_output": self.structured_output,
            "reports_cost": self.reports_cost,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class EngineResult:
    """Normalised outcome of one run, whatever engine produced it."""

    text: str
    exit_code: int
    session_id: str | None = None
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None
    warnings: tuple[str, ...] = ()
    #: Populated when the engine was asked for schema-constrained output.
    structured: dict[str, Any] | None = None
    tokens: TokenUsage | None = None
    model: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and bool(self.text.strip())


@dataclass(frozen=True, slots=True)
class RunSpec:
    """Everything `worker` needs to start one engine process."""

    argv: list[str]
    #: Written by the engine and read back after exit, when it supports one.
    last_message_path: Path | None = None
    #: Files the adapter wants created before launch (schemas, prompt files).
    files: dict[Path, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class EngineAdapter(Protocol):
    engine: Engine

    def probe(self) -> Capabilities | None:
        """Return capabilities, or None when the binary is unusable/absent."""

    def build(self, task: Task, policy: EnginePolicy, workspace: Path, prompt: str, artifacts: Path) -> RunSpec:
        """Build the process spec. `artifacts` is a per-task scratch directory."""

    def parse(self, stdout: str, stderr: str, exit_code: int, spec: RunSpec) -> EngineResult:
        """Turn the captured output into an `EngineResult`."""


def which(binary: str) -> str | None:
    return shutil.which(binary)


def read_version(argv: list[str]) -> str:
    """Run a `--version`-ish command. Returns "unknown" rather than raising."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    output = (completed.stdout or completed.stderr or "").strip()
    return output.splitlines()[0].strip() if output else "unknown"


def read_help(argv: list[str]) -> str:
    """Capture a binary's help text so capabilities can be detected from flags.

    Preferred over version strings because vendor docs routinely describe flags that
    the installed build does not have.
    """
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=20,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"{completed.stdout or ''}\n{completed.stderr or ''}"


def read_usage_int(usage: dict[str, Any], field_name: str) -> int:
    """Read an integer usage field, treating missing or malformed values as zero."""
    value = usage.get(field_name)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
