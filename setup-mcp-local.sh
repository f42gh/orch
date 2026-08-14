#!/usr/bin/env bash
set -euo pipefail

# setup-mcp-local.sh
# - create .venv if missing
# - install editable package with the [mcp] extra
# - install the /orch command template into ~/.claude/commands/orch.md (via orch)
# - print the exact `claude mcp add` command to register this MCP server

PYTHON=${PYTHON:-$(command -v python3 || command -v python || true)}
if [ -z "${PYTHON}" ]; then
  echo "Python 3 not found in PATH. Install Python 3.12+ and retry." >&2
  exit 1
fi

# ensure Python version >= 3.12
if ! "$PYTHON" - <<'PY' >/dev/null 2>&1; then
import sys
if sys.version_info < (3,12):
    raise SystemExit(2)
PY
then
  echo "Python must be >= 3.12. Current: $($PYTHON -V 2>&1)" >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating virtualenv at .venv using ${PYTHON}"
  "$PYTHON" -m venv .venv
else
  echo "Using existing .venv"
fi

# Activate venv in a POSIX-compatible way
# shellcheck source=/dev/null
source .venv/bin/activate

echo "Upgrading pip and installing package with [mcp] extra..."
python -m pip install --upgrade pip
python -m pip install -e ".[mcp]"

# Install the Claude command template into the default location
if command -v orch >/dev/null 2>&1; then
  echo "Installing /orch command template (locale: ja) via orch"
  # non-fatal: continue even if this fails
  orch install-claude-command --locale ja || echo "orch install-claude-command failed (continue)"
else
  echo "orch not found in PATH. The package should have installed it into .venv/bin. Ensure you run this script from the repo root and the venv activation succeeded." >&2
fi

ABS_PY=$(pwd)/.venv/bin/python

cat <<-EOF

==== MCP / Copilot registration ====
Recommended command to register this MCP server with the Copilot/claude CLI:

  claude mcp add orch -s user -- $ABS_PY -m agent_orchestrator.mcp_server

Alternative (if using the repo's "uv" runner):

  claude mcp add orch -s user -- orch mcp   # after: uv tool install -e ".[mcp,api]"

To start the server locally for testing use:

  $ABS_PY -m agent_orchestrator.mcp_server

If the 'claude' CLI is available and you want this script to auto-register the MCP server,
re-run it with REGISTER=1 in the environment (e.g. REGISTER=1 ./setup-mcp-local.sh).

EOF

if [ "${REGISTER:-0}" != "1" ]; then
  echo "Setup complete. To auto-register with claude (if installed): REGISTER=1 ./setup-mcp-local.sh"
  exit 0
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "claude CLI not found; cannot register automatically. Install the claude CLI and re-run with REGISTER=1." >&2
  exit 1
fi

echo "Registering MCP server with claude..."
claude mcp add orch -s user -- $ABS_PY -m agent_orchestrator.mcp_server

echo "Registered. Verify with: claude mcp list"
