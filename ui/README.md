# Agent Orchestrator UI

This is the Deno + React + Vite UI for the local Agent Orchestrator API.

## Requirements

- Deno 2.9 or newer for `deno desktop`.
- The Python API server running on `127.0.0.1:8765`.

## Development

From the repository root:

```bash
uv sync --extra api
uv run agentapi run
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
