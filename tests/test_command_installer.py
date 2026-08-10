from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from agent_orchestrator.command_installer import (
    CommandInstallError,
    command_template,
    default_command_path,
    install_command,
)


def test_default_path_is_the_claude_command_directory() -> None:
    assert default_command_path() == Path.home() / ".claude" / "commands" / "orch.md"


def test_install_creates_parent_and_command(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "orch.md"

    result = install_command(target)

    assert result.path == target
    assert result.changed is True
    assert result.backup_path is None
    assert target.read_text(encoding="utf-8") == command_template()


def test_identical_command_is_a_no_op(tmp_path: Path) -> None:
    target = tmp_path / "orch.md"
    target.write_text(command_template(), encoding="utf-8")
    os.utime(target, ns=(1_000_000_000, 1_000_000_000))
    before = target.stat()

    result = install_command(target)

    after = target.stat()
    assert result.changed is False
    assert result.backup_path is None
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ino == before.st_ino


def test_japanese_template_can_be_installed(tmp_path: Path) -> None:
    target = tmp_path / "orch.md"

    result = install_command(target, locale="ja")

    assert result.changed is True
    assert target.read_text(encoding="utf-8") == command_template("ja")
    assert "argument-hint: <依頼する作業>" in command_template("ja")


def test_unknown_template_locale_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CommandInstallError, match="unsupported command locale"):
        install_command(tmp_path / "orch.md", locale="fr")


def test_different_command_refuses_without_force(tmp_path: Path) -> None:
    target = tmp_path / "orch.md"
    target.write_text("my command\n", encoding="utf-8")

    with pytest.raises(CommandInstallError, match="different content"):
        install_command(target)

    assert target.read_text(encoding="utf-8") == "my command\n"
    assert list(tmp_path.glob("orch.md.backup-*")) == []


def test_force_preserves_unique_backups_and_replaces_target(tmp_path: Path) -> None:
    target = tmp_path / "orch.md"
    target.write_text("first local command\n", encoding="utf-8")

    first = install_command(target, force=True)
    assert first.backup_path is not None
    assert re.fullmatch(
        r"orch\.md\.backup-\d{8}T\d{12}Z-[0-9a-f]{8}", first.backup_path.name
    )
    assert first.backup_path.read_text(encoding="utf-8") == "first local command\n"
    assert target.read_text(encoding="utf-8") == command_template()

    target.write_text("second local command\n", encoding="utf-8")
    second = install_command(target, force=True)

    assert second.backup_path is not None
    assert second.backup_path != first.backup_path
    assert second.backup_path.read_text(encoding="utf-8") == "second local command\n"
    assert first.backup_path.read_text(encoding="utf-8") == "first local command\n"
    assert len(list(tmp_path.glob("orch.md.backup-*"))) == 2


def test_failed_atomic_replace_leaves_original_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "orch.md"
    target.write_text("local command\n", encoding="utf-8")

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("agent_orchestrator.command_installer.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        install_command(target, force=True)

    assert target.read_text(encoding="utf-8") == "local command\n"
    backups = list(tmp_path.glob("orch.md.backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "local command\n"
    assert list(tmp_path.glob(".orch.md.*.tmp")) == []


def test_install_replaces_from_a_temporary_file_in_the_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "orch.md"
    original_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def record_replace(source: str | Path, destination: str | Path) -> None:
        replacements.append((Path(source), Path(destination)))
        original_replace(source, destination)

    monkeypatch.setattr("agent_orchestrator.command_installer.os.replace", record_replace)

    install_command(target)

    assert len(replacements) == 1
    temporary, destination = replacements[0]
    assert temporary.parent == target.parent
    assert destination == target
    assert not temporary.exists()
