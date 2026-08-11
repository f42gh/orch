# Engine capabilities (measured)

Everything below was measured on this machine against a throwaway git repository,
not copied from vendor documentation. Vendor docs describe flags that the locally
installed binaries do not always have, so the adapters trust `probe()` over docs.

Measured on 2026-08-10, macOS arm64.

| Engine | Binary | Version | Structured output | Working dir | stdin |
|---|---|---|---|---|---|
| codex | `codex` | 0.147.0 | JSONL via `--json` | `-C <dir>` | **must be closed** |
| grok | `grok` | 1.0.0 | single JSON via `--output-format json` | `--cwd <dir>` | closed is fine |
| antigravity | `agy` | 1.0.12 → 1.1.11 | none → single JSON (see below) | **`--add-dir <dir>` required** | closed is fine |
| claude | `claude` | 2.1.226 | single JSON via `--output-format json` | process cwd | closed is fine |

`agy` self-updated from 1.0.12 to 1.1.11 midway through this work, which is the reason
capabilities are probed rather than assumed: the adapter moved onto the JSON path on
its own, with no code change. Both shapes stay covered by fixtures.

## codex

```bash
codex exec --json -s read-only -C <workspace> --skip-git-repo-check \
  -o <last_message.md> "<prompt>" < /dev/null
```

**`codex exec` hangs forever if stdin stays open on a non-TTY.** It prints
`Reading additional input from stdin...` to stderr and waits. The worker must pass
`stdin=subprocess.DEVNULL`. This cost one hung 300s probe run to discover and is the
single most important detail in this file.

JSONL event shape actually observed:

```json
{"type":"thread.started","thread_id":"019fea4e-fc67-71f0-b4da-d38860067426"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"..."}}
{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"...","aggregated_output":"","exit_code":null,"status":"in_progress"}}
{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"...","aggregated_output":"...","exit_code":0,"status":"completed"}}
{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"README.md, calc.py"}}
{"type":"turn.completed","usage":{"input_tokens":29879,"cached_input_tokens":25088,"cache_write_input_tokens":0,"output_tokens":175,"reasoning_output_tokens":43}}
```

Notes:

- The session id is `thread_id` at the top level of `thread.started`, not nested under `item`.
- `turn.completed.usage` uses `cached_input_tokens` / `cache_write_input_tokens`
  (different names from grok's `cache_read_input_tokens` / `cache_creation_input_tokens`).
- **codex reports no cost.** `total_cost_usd` does not exist in this stream.
- The final answer is the last `agent_message`, but the adapter prefers the file written
  by `-o` so that a truncated or unparseable stream still yields a result.
- Fixture: `tests/fixtures/codex_exec.jsonl`

## grok

```bash
grok -p "<prompt>" --output-format json --cwd <workspace> \
  --sandbox read-only --no-auto-update --max-turns 6 < /dev/null
```

Single JSON object. Keys observed:
`text`, `thought`, `stopReason`, `sessionId`, `requestId`, `num_turns`, `usage`,
`modelUsage`, `total_cost_usd`, `total_cost_usd_ticks`.

Notes:

- `text` concatenates the agent's preamble with the final answer
  (observed: `"I'll list the repository contents without changing anything.calc.py README.md"`).
  Treat it as prose, not as a clean answer — use `--json-schema` when a machine-readable
  answer is required.
- `total_cost_usd_ticks` is exact integer cost (1 USD = 10^10 ticks). Prefer it over the float.
- `cost_is_partial` / `usage_is_incomplete` did not appear in this run but are documented;
  when either is true all cost floats are omitted, so the adapter must not sum `modelUsage`
  rows into a fake total.
- Exit codes: 0 success, 1 error, 130 SIGINT, 143 SIGTERM.
- Fixture: `tests/fixtures/grok_result.json`

## antigravity (agy)

```bash
agy -p "<prompt>" --add-dir <workspace> < /dev/null
```

Two findings that contradict the public material:

1. **The non-TTY stdout bug (upstream issue #76) does not reproduce on 1.0.12.**
   A plain pipe returned the full response (exit 0, non-empty stdout). No PTY wrapper is
   needed. The adapter therefore uses ordinary pipes and only falls back if output is empty.
2. **`agy` ignores the process working directory.** Started inside the probe repository
   with no flags, it reported that no workspace was set and inspected
   `~/.gemini/antigravity-cli/scratch` instead. `--add-dir <workspace>` is mandatory;
   without it the agent silently works on the wrong tree.
   Fixture of that failure mode: `tests/fixtures/agy_print_no_workspace.txt`

On 1.0.12, `strings $(command -v agy)` confirmed that `--output-format`, `stream-json`
and `--effort` were **absent from the binary** (0 matches) even though the published docs
described them. Plain-text output has no envelope, so there was no session id, usage or
cost. Fixture: `tests/fixtures/agy_print.txt`

1.1.11 added `--output-format`, `--json-schema`, `--effort`, `--agent` and `--mode`, and
`probe()` moved the adapter onto the JSON path automatically. The envelope is:

```json
{
  "conversation_id": "a04753fe-4d39-48a6-bd02-ba88fb0ce1f2",
  "status": "SUCCESS",
  "response": "The repository contains: `.git/`, `README.md` (73 B), `calc.py` (32 B).\n",
  "duration_seconds": 10.313214,
  "num_turns": 1,
  "usage": {"input_tokens": 28469, "output_tokens": 204, "thinking_tokens": 0,
            "cache_read_tokens": 27101, "total_tokens": 28673}
}
```

Notes:

- The answer is `response`, and the session id is `conversation_id`.
- **`status` is the only failure signal**: agy exits 0 on `CANCELED` and `INTERRUPTED`
  as well as `SUCCESS`, so exit code alone will call a cut-short run a success.
- Still no cost field.
- Fixture: `tests/fixtures/agy_print.json`

## claude

```bash
claude -p "<prompt>" --output-format json --permission-mode plan --model sonnet < /dev/null
```

Single JSON object. Keys observed:
`type`, `subtype`, `result`, `is_error`, `stop_reason`, `session_id`, `uuid`,
`num_turns`, `duration_ms`, `duration_api_ms`, `usage`, `modelUsage`, `total_cost_usd`,
`permission_denials`, plus several fast-mode/telemetry fields.

Notes:

- The answer is `result` (not `text`), and `is_error` is the authoritative failure signal.
- `permission_denials` is worth surfacing: it tells the orchestrator that the agent was
  blocked rather than unable.
- Fixture: `tests/fixtures/claude_result.json`

## Token usage normalisation

The native `usage` object remains attached to every result as the debugging record.
Adapters also map it into `TokenUsage`, whose `input_tokens` means non-cached input:

| `TokenUsage` field | codex | grok | claude | antigravity |
|---|---|---|---|---|
| `input_tokens` | `input_tokens - cached_input_tokens` | `input_tokens` | `input_tokens` | `input_tokens - cache_read_tokens` |
| `cache_read_tokens` | `cached_input_tokens` | `cache_read_input_tokens` | `cache_read_input_tokens` | `cache_read_tokens` |
| `cache_write_tokens` | `cache_write_input_tokens` | `cache_creation_input_tokens` | `cache_creation_input_tokens` | absent → 0 |
| `output_tokens` | `output_tokens` | `output_tokens` | `output_tokens` | `output_tokens` |
| `reasoning_tokens` | `reasoning_output_tokens` | `reasoning_tokens` | absent → 0 | `thinking_tokens` |

The inclusion rules come from these recorded outputs:

- Grok excludes cache reads from input. Its fixture has 2,807 input + 24,960 cache
  reads + 139 output = 27,906, exactly the reported `total_tokens`. No subtraction.
- Claude excludes cache reads from input. It reports only 4 `input_tokens` alongside
  43,542 `cache_read_input_tokens`; those cannot be an included subset. No subtraction.
- Antigravity includes cache reads in input. Its 28,469 input + 204 output = 28,673,
  exactly the reported `total_tokens`, while its 27,101 cache reads are smaller than
  input. Subtract the cache reads.
- Codex inclusion is an **assumption**, not a measurement: it reports no total to
  cross-check. The observed 29,879 input with 25,088 cached input follows the OpenAI
  Responses convention where input includes cached tokens. Reading them as separate
  would imply 54,967 input tokens for a one-line prompt. This must be re-verified
  against a future run whose reported total can be cross-checked.

Both subtractions are clamped at zero. A cached count larger than input invalidates the
inclusion rule for that build, so the adapter preserves the result but raises a warning
rather than silently emitting a negative or invented count. A run with no native usage
object keeps `tokens=None`; an explicitly reported all-zero usage remains distinguishable.

## Consequences for the adapters

- Every engine is spawned with `stdin=DEVNULL`. Required by codex, harmless elsewhere.
- Working directory is passed explicitly per engine (`-C`, `--cwd`, `--add-dir`), never
  inherited, because agy proves inheritance is not reliable.
- Field names for usage and cost differ per engine, so each adapter normalises into
  `EngineResult`; only grok and claude can report cost at all.
- Capability detection is `probe()`-based, never version-string or doc based.
