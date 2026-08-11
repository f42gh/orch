"""The single prompt every engine receives.

Kept engine-agnostic on purpose: the same task text must mean the same thing whichever
CLI ends up running it, otherwise comparing two engines' diffs tells you nothing.
"""

from __future__ import annotations

from agent_orchestrator.models import Risk, Task, TaskKind
from agent_orchestrator.router import AccessLevel


KIND_INSTRUCTIONS: dict[TaskKind, str] = {
    TaskKind.IMPLEMENT: """
## How to approach this task
- Implement it.
- Match the existing code style, naming, and the way tests are written here.
- Add tests where you can, and run them.
""",
    TaskKind.REFACTOR: """
## How to approach this task
- Change the structure only. Behaviour must stay identical.
- Confirm the tests produce the same result before and after.
- Do not add features.
""",
    TaskKind.TEST: """
## How to approach this task
- Add or fix tests.
- Actually run them, and report the result.
- Do not change what the production code is specified to do in order to make a test pass.
""",
    TaskKind.REVIEW: """
## How to approach this task
- Review only. Do not modify any file.
- Base every finding on code you actually read, not on what you assume it does.
- For each finding give the file and line, what is wrong, and how it breaks.
- If you conclude there is nothing wrong, say so rather than inventing a finding.
""",
    TaskKind.INVESTIGATE: """
## How to approach this task
- Investigate only. Do not modify any file.
- Cite the paths of the files you actually read as evidence.
- Report what you established, what you could not, and what to check next, separately.
""",
    TaskKind.UI_VERIFY: """
## How to approach this task
- Drive the interface and see what it does.
- Report what behaved as expected and what did not, separately.
- Write the reproduction steps at a level of detail someone else can follow verbatim.
""",
}


ACCESS_INSTRUCTIONS: dict[AccessLevel, str] = {
    AccessLevel.READ_ONLY: """
## Permissions
This task is read-only.
Do not create, edit, or delete any file.
""",
    AccessLevel.WORKSPACE_WRITE: """
## Permissions
You may modify files inside the working directory only.
Never write anything outside the working directory.
""",
    AccessLevel.FULL: """
## Permissions
The sandbox is disabled. This only happens when the configuration explicitly allows it.
Even so, do not modify anything outside the working directory.
""",
}


COMMON_RULES = """
## Rules
- Do not modify files outside the working directory
- When something is unclear, investigate the codebase before asking
- Do not perform destructive operations
- Do not read secrets, tokens, or private keys
- Do not run git commit or git push (a human reviews the diff)
- Do not deploy
"""

PROSE_OUTPUT_RULES = """
## Report at the end
- What you changed, or that you changed nothing
- The commands you ran and their results
- What a human should check
- Remaining risks and unresolved problems
"""

#: Used whenever the engine is also constrained by a JSON schema.
#:
#: Asking for the prose sections above *and* enforcing a closed schema puts the model in
#: a bind it cannot satisfy: observed with grok, which emitted a schema-shaped object
#: every turn and never terminated, burning the whole turn budget before being
#: cancelled. The two instructions must not both be present.
STRUCTURED_OUTPUT_RULES = """
## Output format
Answer with **exactly one JSON object** that strictly matches the given JSON schema.
- No prose before or after it, and no code fences
- Do not emit more than one JSON object
- Do not add keys the schema does not define
- If there is nothing to report, leave findings as an empty array and explain why in summary
Investigate as much as you need to; the final output is this single JSON object and nothing else.
"""


def build_prompt(
    task: Task,
    access: AccessLevel = AccessLevel.WORKSPACE_WRITE,
    structured: bool = False,
) -> str:
    """Compose the prompt for `task`.

    `access` comes from the router rather than from the task so that the prompt always
    agrees with the sandbox the process is actually started under. `structured` must be
    true whenever the engine is being given a JSON schema, so the prompt asks for the
    schema's shape instead of contradicting it.
    """
    high_risk_note = ""
    if task.risk == Risk.HIGH:
        high_risk_note = """
## Caution
This task is high risk.
Implementing, editing, and deleting are all forbidden.
Do only investigation, blast-radius analysis, an implementation plan, and risk analysis.
"""

    output_rules = STRUCTURED_OUTPUT_RULES if structured else PROSE_OUTPUT_RULES

    return f"""You are a coding agent running in a local development environment.

## Task
{task.task}

## Working directory
{task.workspace_path}

## Kind
{task.kind.value}

## Risk level
{task.risk.value}
{KIND_INSTRUCTIONS[task.kind]}{ACCESS_INSTRUCTIONS[access]}{high_risk_note}{COMMON_RULES}{output_rules}"""


#: Schema handed to engines that can constrain their final answer. Used for review so the
#: orchestrator gets findings it can act on instead of prose it has to re-read.
REVIEW_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "findings"],
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "file", "summary", "failure"],
                "properties": {
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "summary": {"type": "string"},
                    "failure": {"type": "string"},
                },
            },
        },
    },
}


def schema_for(kind: TaskKind) -> dict[str, object] | None:
    return REVIEW_SCHEMA if kind == TaskKind.REVIEW else None
