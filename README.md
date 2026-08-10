# agent-orchestrator

`agent-orchestrator` is a local Python task runner that keeps a lightweight daemon alive and starts an isolated Claude Agent SDK worker for each queued task. It is designed for local development, research, and documentation tasks where workspace, session, logs, and permissions should be separated per task.

The daemon is the resident process. Claude is invoked per task as a worker, with a dedicated `session_id`, git worktree, log directory, and risk policy.

## Setup

```bash
uv sync
```

For real Claude worker execution, install the Claude SDK extra as well:

```bash
uv sync --extra claude
```

Claude Code must also be authenticated and usable on the machine. This project imports the Python package as:

```python
from claude_code_sdk import ClaudeCodeOptions, query
```

The worker builds options with `cwd` and `allowed_tools`, and filters optional fields against the installed SDK signature so small SDK API differences fail less dramatically.

## Runtime Directory

By default, runtime state is stored under:

```text
~/agent-runtime/
  tasks.db
  logs/
  repos/
  workspaces/
  sessions/
```

Override it with either `--runtime-root` or `AGENT_ORCHESTRATOR_RUNTIME_ROOT`.

## Add A Task

```bash
uv run agentctl add \
  --repo ~/dev/my-project \
  --task "READMEのセットアップ手順を最新化して" \
  --risk normal
```

Options:

```text
--repo        target git repository path
--task        task text
--risk        read_only / normal / high
--priority    low / normal / high
```

## Run The Daemon

```bash
uv run agentd run
```

For a single queued task, useful during development:

```bash
uv run agentd run --once
```

`agentctl daemon` is also available, but this README uses `agentd run` as the primary command.

## Run The Local API

The GUI talks to a local FastAPI server. Install the API extra and start it:

```bash
uv sync --extra api
uv run agentapi run
```

By default it listens on `127.0.0.1:8765`.

Useful endpoints:

```text
GET  /health
GET  /tasks
POST /tasks
GET  /tasks/{task_id}
POST /daemon/process-one
GET  /tasks/{task_id}/logs/{agent|stdout|stderr}
GET  /tasks/{task_id}/diff
GET  /tasks/{task_id}/result
```

## Run The React UI

The `ui/` directory contains a Deno + React + Vite app. Install Deno 2.9 or newer, start the Python API, then run:

```bash
cd ui
deno task dev
```

For the Deno Desktop wrapper:

```bash
cd ui
deno task desktop
```

The v1 desktop app assumes the Python API is already running and does not bundle Python, uv, or Claude credentials.

## Check Status

```bash
uv run agentctl list
uv run agentctl show task-0001
```

Statuses:

```text
queued
running
blocked
failed
succeeded
needs_review
```

## Logs And Diff

Each task writes files under:

```text
~/agent-runtime/logs/{task_id}/
  agent.log
  stdout.log
  stderr.log
  diff.patch
  result.json
```

`diff.patch` is captured with `git diff` after the worker exits. `result.json` includes task id, status, workspace path, diff path, summary, warnings, and whether human review is required.

## Risk Policy

`read_only` allows reading and search-oriented tools. Edits, writes, commits, pushes, package installs, and deletes are not intended for this mode.

`normal` allows coding-oriented tools such as read, edit, write, grep, glob, bash, test, lint, and format, subject to guardrails.

`high` is planning-only in v0. The worker prompt explicitly forbids implementation, editing, and deletion, and asks only for investigation, impact analysis, implementation plan, risks, and open questions.

## Guardrails

The v0 guardrail detector blocks obvious dangerous command patterns, including:

```text
rm -rf /
sudo
chmod -R 777
curl ... | bash
wget ... | bash
git push
deploy
.env
id_rsa
private_key
secret
token
```

This is intentionally a first line of defense, not a complete sandbox.

## Known Limitations

- This tool is not a fully automatic safe developer.
- Generated changes must always be reviewed by a human.
- `git push` and deploy are not performed by v0.
- High risk tasks produce a plan only and do not implement changes.
- Do not use this for workflows that require reading secrets or private keys.
- Concurrent edits to the same file can still conflict.
- SDK hook-level command blocking uses the Claude SDK `can_use_tool` hook in v0, but the detector is still intentionally simple.

## Future Work

- Add approval workflows for package upgrades, migrations, external API calls, and push operations.
- Add file-level locks for concurrent tasks against the same repository.
- Store structured SDK events in an events table.
- Add SDK hook integration when the installed Claude Agent SDK exposes a stable hook API.
- Add a richer router while keeping the daemon as the only resident process.
