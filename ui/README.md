# Agent Orchestrator UI

> **Legacy:** This Deno + React + Vite UI remains available for the original single-task
> Agent Orchestrator API. It does not create or manage Runs and Batches. Use the MCP
> tools or `orch` for new workflow orchestration.

## Requirements

- Deno 2.9 or newer for `deno desktop`.
- The Python API server running on `127.0.0.1:8765`.

## Development

From the repository root:

```bash
uv sync --extra api
orch api run
```

In another terminal:

```bash
cd ui
deno task dev
```

Open the Vite URL shown in the terminal.

## Desktop

From `ui/`:

```bash
deno task desktop
```

The desktop app uses the same React UI and connects to `http://127.0.0.1:8765`.

## Checks

```bash
deno task check
deno test src/api.test.ts
```
