# orch

[English](README.md) | 日本語

コーディング作業を適材適所のエージェント CLI に振り分けるローカルオーケストレーター。
各タスクは専用の git worktree で実行され、結果は人間がレビューするために返される。

Claude Code がオーケストレーターとなり、`codex`・`grok`・`agy`(Antigravity)・`claude` が
MCP サーバー経由で呼び出されるワーカーになる。タスクの *kind* は自動選択時の
デフォルトエンジンを決める:

| kind | engine | アクセス |
|---|---|---|
| `implement` / `refactor` / `test` | codex | 書き込み可(worktree 内のみ) |
| `review` / `investigate` | grok | 読み取り専用 |
| `ui_verify` | antigravity | 書き込み可(worktree 内のみ) |
| (上記すべてのフォールバック) | claude | |

未インストールのエンジンには自動でフォールバックするため、エンジンが欠けていても失敗せず
縮退動作になる。各 CLI が実際に何をするかは、ドキュメントの記述ではなく実測に基づいて
`docs/engine-capabilities.md` に記録している。

新しいワークフローでは、このデフォルトを明示的なルート表で上書きできる。実行を始める前に
一度だけ割り当てを決める:

| ワークフロー | 適している場合 | ライフサイクル |
|---|---|---|
| **Run** | 後から作業を追加する、または先行結果を見て次の作業を決める | 保存され、close するまで追加可能 |
| **Batch** | 独立した全タスクが最初から分かっている | 一度だけ投入し、後から追加しない |

ルート表が決めるのはエンジンだけである。プロンプト、出力スキーマ、アクセス権は引き続き
タスクの kind と risk で決まるため、別のエンジンを割り当てても安全ポリシーは迂回できない。

Run が保存するのはルートとタスク履歴であり、あるタスクの未コミットな worktree を次のタスクへ
自動で重ねるものではない。後続作業がそのコードを必要とする場合は、先に人間がレビューして
採用・コミットし、その commit を `base_ref` に指定する。

## セットアップ

```bash
git clone https://github.com/f42gh/orch
cd orch
uv sync
uv run agentctl engines   # このマシンにあるエンジンとルーティングテーブルを表示
```

各 CLI のインストールと認証はそれぞれ個別に必要。このツールが認証情報を保存することはない。

### Claude Code から使う

```bash
claude mcp add orch -s user -- uv run --directory ~/dev/orch agentmcp
```

あとは `/orch <やってほしいこと>` と入力するか、ツールを直接呼び出す。
ファンアウトや、あるエンジンに実装させて別のエンジンにレビューさせるパターンなど、
知っておくと便利な使い方は `docs/CLAUDE-PLAYBOOK.md` を参照。

Claude Code から Run/Batch の確認手順を確実に使うには、このリポジトリに含まれる
`/orch` コマンドテンプレートをインストールする:

```bash
uv run agentctl install-claude-command
```

デフォルトのインストール先は `~/.claude/commands/orch.md`。内容が同一なら何も変更しない。
別内容の既存コマンドは `--force` なしでは上書きせず、強制置換時も一意な名前のバックアップを
先に作成する。別の場所には `--target PATH` でインストールできる。

## ターミナルから使う

後から作業を追加する場合は永続 Run を作る:

```bash
uv run agentctl run create --repo ~/dev/my-project \
  --route implement=codex \
  --fallback implement=claude,grok
uv run agentctl run dispatch run-0001 --task "パーサーを追加して" --kind implement
uv run agentctl run show run-0001
uv run agentctl run close run-0001
```

独立した全タスクが分かっている場合は、一回限りの Batch で投入する。tasks file はタスク仕様の
JSON 配列で、`--tasks-file -` を指定すると標準入力から読み込む。

```bash
uv run agentctl batch dispatch --repo ~/dev/my-project \
  --route implement=codex \
  --fallback implement=claude,grok \
  --tasks-file tasks.json
```

割り当てる kind ごとに `--route KIND=ENGINE` を繰り返す。省略した kind は、ワークフロー作成時の
自動 primary を継承する。入力では `agy` を別名として受け付けるが、保存値と出力では常に
正規名 `antigravity` を使う。

フォールバックは、リストを指定したかどうかで挙動が明確に変わる:

- kind の `--fallback` を省略すると、その時点の自動ルート順を保存する。後の実行時にその
  候補がすべて利用不能なら、自動モードは別のインストール済みエンジンを決定的な順で選ぶ。
- 空でない順序付きリストを指定すると、その順序だけを厳密かつ網羅的に使う。primary と
  リスト内のどのエンジンもインストールされていなければ、別のエンジンを暗黙に選ばず失敗する。
  空リストは無効。

### 従来の単一タスクコマンド

元からある単一タスクコマンドも引き続き利用できる:

```bash
uv run agentctl add --repo ~/dev/my-project --task "READMEのセットアップ手順を最新化して"
uv run agentctl add --repo ~/dev/my-project --task "calc.py をレビューして" --kind review --risk read_only
uv run agentctl dispatch --repo ~/dev/my-project --task "..." --json  # add と開始を一発で。CAGE が呼ぶのはこれ

uv run agentd run-task task-0001      # 1 件実行
uv run agentd run --max-concurrency 2 # キューを消化

uv run agentctl list
uv run agentctl show task-0001
```

`add` と `dispatch` のオプション: `--kind`、`--engine`、`--risk`、`--priority`、`--parent`、`--base-ref`。

## ランタイムの構成

```text
~/agent-runtime/
  tasks.db                     MCP サーバー・デーモン・API・UI で共有
  workspaces/<task_id>/repo    git worktree(ブランチは agent/<engine>/<task_id>)
  logs/<task_id>/
    agent.log stdout.log stderr.log
    diff.patch result.json
```

`--runtime-root` または `AGENT_ORCHESTRATOR_RUNTIME_ROOT` で上書きできる。

完了したタスクは `succeeded` ではなく必ず `needs_review` になる — 自分の成果物を自分で
完了扱いにするものはいない。`result.json` には、要約、レビューの場合は構造化された指摘、
変更ファイル、diffstat、トークン使用量、エンジンが報告する場合はコスト、警告が入る。

## Legacy HTTP API と UI

HTTP API と React UI は、従来の単一タスクワークフロー用として引き続き利用できる。
Run と Batch の作成・管理には対応しないため、新しいワークフローのオーケストレーションには
MCP ツールまたは `agentctl` を使う。

```bash
uv run agentapi run            # 127.0.0.1:8765
cd ui && deno task dev
```

## リスクとアクセス制御

`--risk` は kind とは独立に、タスクに許す操作の範囲を決める:

- `read_only` — 読み取りと検索のみ。何も書き込まない。
- `normal` — 書き込み可。ただし worktree 内に限る。
- `high` — 計画立案のみ。プロンプトで実装を禁止し、分析を求める。

封じ込めは 4 層構成。CLI エンジンには Claude SDK のようにツール呼び出しをプロセス内で
ブロックするフックがないためである:

1. 各エンジンが提供する OS サンドボックス(macOS では Seatbelt によるカーネル強制)、
2. `git push` や `sudo` などに対するエンジンの deny ルール、
3. git worktree による分離。元のチェックアウトには決して触れない、
4. 実行後の diff とログのスキャン。ブロックはせず、結果に注記を付ける。

サンドボックスなしのアクセスは `routing.toml` での明示的なオプトインが必要。
`--dangerously-*` 系のフラグに勝手に手を伸ばすことはない。

## ルーティングのチューニング

`~/.config/agent-orchestrator/routing.toml` は任意。書いたキーだけが上書きされる。この表は
自動選択のデフォルトを提供する。Run と Batch は作成時に具体化したルートを保存するため、
後からこのファイルを変更しても既存ワークフローは変わらない:

```toml
[kinds.implement]
engine = "grok"
fallbacks = ["codex", "claude"]

[risk.normal]
max_turns = 60
timeout_s = 2400

[engines.codex]
allow_dangerous = true   # codex のサンドボックスを外す。設定する前によく考えること
```

## 制限事項

- 生成された変更は必ず人間が読む必要がある。`orch_adopt` はデフォルトでパッチを返すだけで、
  何も書き込まない。
- コミット・プッシュ・デプロイは一切しない。
- `high` リスクは計画を出すだけで、実装はしない。
- 並行タスクは実行中は分離されているが、同じ関数を編集した 2 つのパッチは
  採用時に衝突する。
- 実行コストを報告するのは grok と claude のみ。codex と antigravity は報告しないため、
  合計は原理的に部分的な値になる。
