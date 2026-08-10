from __future__ import annotations

import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path


COMMAND_RELATIVE_PATH = Path(".claude/commands/orch.md")


class CommandInstallError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InstallResult:
    path: Path
    changed: bool
    backup_path: Path | None = None


def default_command_path() -> Path:
    return Path.home() / COMMAND_RELATIVE_PATH


def command_template() -> str:
    template = resources.files("agent_orchestrator").joinpath("templates", "orch.md")
    return template.read_text(encoding="utf-8")


def install_command(target: Path | None = None, *, force: bool = False) -> InstallResult:
    path = (target or default_command_path()).expanduser()
    content = command_template().encode("utf-8")
    backup_path: Path | None = None
    existing_mode: int | None = None

    if path.exists():
        if not path.is_file():
            raise CommandInstallError(f"command target is not a file: {path}")
        if path.read_bytes() == content:
            return InstallResult(path=path, changed=False)
        if not force:
            raise CommandInstallError(
                f"command already exists with different content: {path}; "
                "use force=True to back it up and replace it"
            )
        existing_mode = stat.S_IMODE(path.stat().st_mode)
        backup_path = _back_up(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, content, mode=existing_mode)
    return InstallResult(path=path, changed=True, backup_path=backup_path)


def _back_up(path: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    while True:
        candidate = path.with_name(
            f"{path.name}.backup-{timestamp}-{uuid.uuid4().hex[:8]}"
        )
        try:
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        break

    try:
        with path.open("rb") as source, os.fdopen(descriptor, "wb") as destination:
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
        shutil.copystat(path, candidate)
    except BaseException:
        candidate.unlink(missing_ok=True)
        raise
    return candidate


def _atomic_write(path: Path, content: bytes, *, mode: int | None) -> None:
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode if mode is not None else 0o644)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
