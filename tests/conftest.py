from __future__ import annotations

from pathlib import Path

import pytest

from orch.config import ROUTING_PATH_ENV

#: Test data only: the code ships no engine preference, so routing tests need a table.
ROUTING_TOML = """
[kinds.implement]
engine = "codex"
fallbacks = ["claude", "grok"]

[kinds.refactor]
engine = "codex"
fallbacks = ["grok", "claude"]

[kinds.test]
engine = "codex"
fallbacks = ["claude", "grok"]

[kinds.review]
engine = "grok"
fallbacks = ["codex", "claude"]

[kinds.investigate]
engine = "grok"
fallbacks = ["claude", "codex"]

[kinds.ui_verify]
engine = "antigravity"
fallbacks = ["claude"]
"""


@pytest.fixture(autouse=True)
def routing_file(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    # Outside tmp_path: some tests assert on every file their tmp_path holds.
    path = tmp_path_factory.mktemp("routing") / "routing.toml"
    path.write_text(ROUTING_TOML)
    monkeypatch.setenv(ROUTING_PATH_ENV, str(path))
    return path
