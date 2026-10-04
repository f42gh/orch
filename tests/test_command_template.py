from __future__ import annotations

from orch.command_installer import command_template


REQUIRED_TOOLS = (
    "orch_engines",
    "orch_routing_set",
    "orch_dispatch",
    "orch_run_create",
    "orch_run_dispatch",
    "orch_run_close",
    "orch_batch_dispatch",
    "orch_workflow_list",
    "orch_workflow_show",
    "orch_status",
    "orch_wait",
    "orch_result",
    "orch_diff",
    "orch_adopt",
)


def test_template_allows_workflow_and_review_tools() -> None:
    template = command_template()
    frontmatter = template.split("---", 2)[1]

    for tool in REQUIRED_TOOLS:
        assert f"mcp__orch__{tool}" in frontmatter
    for tool in (
        "Read",
        "Grep",
        "Glob",
        "Bash(git status:*)",
        "Bash(git diff:*)",
        "AskUserQuestion",
    ):
        assert f"  - {tool}" in frontmatter


def test_template_requires_confirmation_and_complete_review_flow() -> None:
    template = command_template()

    assert "$ARGUMENTS" in template
    assert "Before dispatching anything" in template
    assert "explicitly confirm" in template
    assert "tasks may be added later" in template
    assert "complete set of independent tasks" in template
    assert "strict manual order" in template
    assert "manual list is exhaustive" in template
    assert "spawn_error" in template
    assert "do not transfer uncommitted worktree changes" in template
    assert "orch_engines" in template
    assert "orch_wait" in template
    assert "orch_result" in template
    assert "orch_diff" in template
    assert "orch_adopt" in template


def test_japanese_template_localizes_the_complete_workflow() -> None:
    template = command_template("ja")
    frontmatter = template.split("---", 2)[1]

    assert "description: orchワークフロー" in frontmatter
    assert "argument-hint: <依頼する作業>" in frontmatter
    assert "$ARGUMENTS" in template
    assert "ユーザーへの説明、確認、最終報告は日本語で行う" in template
    assert "明示的な確認" in template
    assert "コミットされていない worktree の変更を引き継がない" in template
    assert "spawn_error" in template
    for tool in REQUIRED_TOOLS:
        assert f"mcp__orch__{tool}" in frontmatter
