"""The single prompt every engine receives.

Kept engine-agnostic on purpose: the same task text must mean the same thing whichever
CLI ends up running it, otherwise comparing two engines' diffs tells you nothing.
"""

from __future__ import annotations

from agent_orchestrator.models import Risk, Task, TaskKind
from agent_orchestrator.router import AccessLevel


KIND_INSTRUCTIONS: dict[TaskKind, str] = {
    TaskKind.IMPLEMENT: """
## このタスクの進め方
- 実装してください。
- 既存のコードスタイル、命名、テストの書き方に合わせてください。
- 可能ならテストを追加し、実行してください。
""",
    TaskKind.REFACTOR: """
## このタスクの進め方
- 振る舞いを変えずに構造だけを直してください。
- リファクタ前後でテストが同じ結果になることを確認してください。
- 機能追加はしないでください。
""",
    TaskKind.TEST: """
## このタスクの進め方
- テストを追加・修正してください。
- テストを実際に実行し、結果を報告してください。
- テストを通すためにプロダクションコードの仕様を変えないでください。
""",
    TaskKind.REVIEW: """
## このタスクの進め方
- レビューだけを行い、ファイルは一切変更しないでください。
- 推測ではなく、実際のコードを読んで指摘してください。
- 各指摘について「ファイル:行」「何が問題か」「どう壊れるか」を示してください。
- 問題が無いと判断した場合は、無理に指摘を作らないでください。
""",
    TaskKind.INVESTIGATE: """
## このタスクの進め方
- 調査だけを行い、ファイルは一切変更しないでください。
- 実際に読んだファイルのパスを根拠として示してください。
- 分かったこと、分からなかったこと、次に確認すべきことを分けて報告してください。
""",
    TaskKind.UI_VERIFY: """
## このタスクの進め方
- 実際に画面を動かして確認してください。
- 期待どおりに動いた点と、動かなかった点を分けて報告してください。
- 再現手順を、他の人がそのまま追える粒度で書いてください。
""",
}


ACCESS_INSTRUCTIONS: dict[AccessLevel, str] = {
    AccessLevel.READ_ONLY: """
## 権限
このタスクは読み取り専用です。
ファイルの作成・編集・削除はしないでください。
""",
    AccessLevel.WORKSPACE_WRITE: """
## 権限
作業ディレクトリ内のファイルだけ変更できます。
作業ディレクトリの外には一切書き込まないでください。
""",
    AccessLevel.FULL: """
## 権限
サンドボックスは無効化されています。設定で明示的に許可された場合のみこの状態になります。
それでも作業ディレクトリの外を変更しないでください。
""",
}


COMMON_RULES = """
## 守ること
- 作業ディレクトリの外のファイルを変更しない
- 不明点があっても、まずコードベースを調査する
- 破壊的操作をしない
- secret, token, private key を読まない
- git commit / git push をしない（差分は人間がレビューする）
- deploy しない
"""

PROSE_OUTPUT_RULES = """
## 最後に必ず出力すること
- 変更した内容（変更していない場合はその旨）
- 実行したコマンドとその結果
- 人間が確認すべき点
- 残っているリスクと未解決の問題
"""

#: Used whenever the engine is also constrained by a JSON schema.
#:
#: Asking for the prose sections above *and* enforcing a closed schema puts the model in
#: a bind it cannot satisfy: observed with grok, which emitted a schema-shaped object
#: every turn and never terminated, burning the whole turn budget before being
#: cancelled. The two instructions must not both be present.
STRUCTURED_OUTPUT_RULES = """
## 出力形式
回答は、指定された JSON スキーマに厳密に一致する **JSON オブジェクトを 1 個だけ** 返してください。
- 散文の前置き・後書き・コードフェンスを付けない
- JSON オブジェクトを複数出力しない
- スキーマに無いキーを追加しない
- 指摘が無い場合は findings を空配列にし、summary にその判断理由を書く
調査は必要なだけ行って構いませんが、最終出力はこの JSON 1 個だけです。
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
## 注意
このタスクは high risk です。
実装・編集・削除は禁止です。
調査、影響範囲の整理、実装計画、リスク分析だけを行ってください。
"""

    output_rules = STRUCTURED_OUTPUT_RULES if structured else PROSE_OUTPUT_RULES

    return f"""あなたはローカル開発環境で動く coding agent です。

## タスク
{task.task}

## 作業ディレクトリ
{task.workspace_path}

## 種別
{task.kind.value}

## リスクレベル
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
