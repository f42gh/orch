from agent_orchestrator.guardrails import is_command_blocked


def test_blocks_rm_rf_root() -> None:
    blocked, reason = is_command_blocked("rm -rf /")
    assert blocked is True
    assert reason is not None


def test_blocks_sudo() -> None:
    blocked, _ = is_command_blocked("sudo make install")
    assert blocked is True


def test_blocks_git_push() -> None:
    blocked, _ = is_command_blocked("git push origin main")
    assert blocked is True


def test_allows_pytest() -> None:
    blocked, reason = is_command_blocked("pytest")
    assert blocked is False
    assert reason is None
