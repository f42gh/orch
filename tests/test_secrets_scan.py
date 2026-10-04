from __future__ import annotations

from pathlib import Path

from orch.config import Config
from orch.logging_utils import task_log_dir
from orch.secrets_scan import scan_diff, scan_logs, scan_task_artifacts, shannon_entropy


def test_flags_a_credential_added_by_the_agent() -> None:
    diff = "+++ b/config.py\n+AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n"

    findings = scan_diff(diff)

    assert any("AWS access key" in finding for finding in findings)


def test_ignores_credentials_the_agent_only_removed() -> None:
    # A removed line is the agent cleaning up, which is the opposite of a leak.
    diff = "--- a/config.py\n-AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'\n"

    assert scan_diff(diff) == []


def test_ignores_the_file_header_that_starts_with_plus() -> None:
    assert scan_diff("+++ b/secret_token_manager.py\n") == []


def test_flags_a_hardcoded_assignment() -> None:
    diff = '+api_key = "abcdef0123456789abcdef0123456789"\n'

    findings = scan_diff(diff)

    assert findings


def test_flags_a_private_key_block() -> None:
    diff = "+-----BEGIN RSA PRIVATE KEY-----\n"

    assert any("private key" in finding for finding in scan_diff(diff))


def test_ordinary_code_is_not_flagged() -> None:
    diff = (
        "+def compute_total(items):\n"
        "+    return sum(item.price for item in items)\n"
        "+# this handles the token bucket rate limiter\n"
    )

    assert scan_diff(diff) == []


def test_entropy_separates_random_strings_from_prose() -> None:
    assert shannon_entropy("aaaaaaaaaaaaaaaa") < 1.0
    assert shannon_entropy("Kj8mQ2xR7vN4pL9wZ3tY6bH1cF5gD0sA") > 4.0


def test_logs_reveal_an_attempted_escape() -> None:
    findings = scan_logs("running: git push origin main\n")

    assert any("git push" in finding for finding in findings)


def test_scan_reads_a_finished_task_from_disk(tmp_path: Path) -> None:
    config = Config(runtime_root=tmp_path)
    log_dir = task_log_dir(config, "task-0001")
    (log_dir / "diff.patch").write_text(
        "+++ b/app.py\n+token = 'ghp_012345678901234567890123456789012345'\n", encoding="utf-8"
    )
    (log_dir / "stdout.log").write_text("$ sudo rm -rf /opt\n", encoding="utf-8")

    findings = scan_task_artifacts(config, "task-0001")

    assert any("GitHub personal access token" in finding for finding in findings)
    assert any("sudo" in finding and "stdout.log" in finding for finding in findings)


def test_scan_of_a_task_with_no_artifacts_is_quiet(tmp_path: Path) -> None:
    assert scan_task_artifacts(Config(runtime_root=tmp_path), "task-0404") == []
