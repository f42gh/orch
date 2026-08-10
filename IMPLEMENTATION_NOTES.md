# Implementation Notes

v0 keeps the orchestration layer simple:

- SQLite stores task queue state.
- `agentd run` claims one queued task at a time and creates a git worktree.
- The Claude worker imports `claude_code_sdk` at execution time.
- Logs and result files are stored per task.
- `high` risk tasks are prompted as planning-only.

The local environment did not have the Python SDK installed when this project was created. The worker therefore fails clearly at runtime if `claude-code-sdk` is missing, while the rest of the orchestrator remains testable.
