from __future__ import annotations

import re


BLOCK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+-[^\n]*r[^\n]*f[^\n]*\s+/(?:\s|$)"), "refuses recursive deletion of filesystem root"),
    (re.compile(r"(^|\s)sudo(\s|$)"), "sudo is not allowed"),
    (re.compile(r"\bchmod\s+-R\s+777\b"), "chmod -R 777 is not allowed"),
    (re.compile(r"\bcurl\b[^\n|]*\|\s*bash\b"), "curl piped to bash is not allowed"),
    (re.compile(r"\bwget\b[^\n|]*\|\s*bash\b"), "wget piped to bash is not allowed"),
    (re.compile(r"\bgit\s+push\b"), "git push requires human approval"),
    (re.compile(r"(^|\s)(deploy|kubectl\s+apply|terraform\s+apply)(\s|$)"), "deploy-like commands require human approval"),
    (re.compile(r"(^|/)\.env(\s|$|/)"), ".env files must not be read without approval"),
    (re.compile(r"(^|/)id_rsa(\s|$|/)"), "private SSH keys must not be read"),
    (re.compile(r"\bprivate[_-]?key\b", re.IGNORECASE), "private keys must not be read"),
    (re.compile(r"\bsecret\b", re.IGNORECASE), "secrets must not be read"),
    (re.compile(r"\btoken\b", re.IGNORECASE), "tokens must not be read"),
]


def is_command_blocked(command: str) -> tuple[bool, str | None]:
    normalized = command.strip()
    for pattern, reason in BLOCK_PATTERNS:
        if pattern.search(normalized):
            return True, reason
    return False, None
