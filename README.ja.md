# orch

[English](README.md) | 日本語

コーディング作業を適材適所のエージェント CLI に振り分けるローカルオーケストレーター。
各タスクは専用の git worktree で実行され、結果は人間がレビューするために返される。

Claude Code がオーケストレーターとなり、`codex`・`grok`・`agy`(Antigravity)・`claude` が
MCP サーバー経由で呼び出されるワーカーになる。Run・Batch・単一タスクのいずれでも、利用ごとに
エンジンの割り当てを選べる。kind を自動ルーティングのままにした場合は、次のデフォルトを使う:

| kind | 自動ルート | アクセス |
|---|---|---|
| `implement` | codex → claude → grok | 書き込み可(worktree 内のみ) |
| `refactor` | codex → grok → claude | 書き込み可(worktree 内のみ) |
| `test` | codex → claude → grok | 書き込み可(worktree 内のみ) |
| `review` | grok → codex → claude | 読み取り専用 |
| `investigate` | grok → claude → codex | 読み取り専用 |
| `ui_verify` | antigravity → claude | 書き込み可(worktree 内のみ) |

自動モードでは、保存された候補が尽きても別のインストール済みエンジンを決定的な順で選ぶため、
少なくとも1つのワーカー CLI が利用できれば処理を続けられる。各 CLI が実際に何をするかは、
ドキュメントではなく実測に基づく[エンジン能力表](docs/engine-capabilities.md)に記録している。

この表は固定割り当てではなくデフォルトである。対話式の `agentctl start`、単一タスクへの厳密な
`--engine` 指定、または Run/Batch のルート表で自由に上書きできる。

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

必要なものは Python 3.12 以上、`uv`、Git、およびインストール・認証済みのワーカー CLI が
少なくとも1つ。Claude Code は MCP 経由で利用する場合だけ必要になる。

```bash
git clone https://github.com/f42gh/orch
cd orch
uv sync
uv run agentctl engines   # このマシンにあるエンジンとルーティングテーブルを表示
```

各 CLI のインストールと認証はそれぞれ個別に必要。このツールが認証情報を保存することはない。

### Claude Code から使う

MCP サーバーを登録し、リポジトリに含まれる `/orch` コマンドテンプレートをインストールする:

```bash
claude mcp add orch -s user -- uv run --directory /absolute/path/to/orch agentmcp
uv run agentctl install-claude-command
```

新しい Claude Code セッションを開始し、`/orch <やってほしいこと>` と入力する。`/orch` は何も
実行する前に、利用可能なエンジン、Run/Batch の案、割り当てを提示し、確認を待つ。MCP ツールを
直接呼び出すこともできる。`/absolute/path/to/orch` はこの checkout の絶対パスに置き換える。
インストーラが配置するのはコマンドファイルだけで、MCP サーバーの登録は行わない。また、
コマンドは上記の登録名 `orch` を前提とする。

直接使うツールは、Run 用の `orch_run_create`・`orch_run_dispatch`・`orch_run_close`、Batch 用の
`orch_batch_dispatch`、再開・確認用の `orch_workflow_list`・`orch_workflow_show`。ファンアウトや、
あるエンジンに実装させて別のエンジンにレビューさせるパターンは
[Claude Code playbook](docs/CLAUDE-PLAYBOOK.md)を参照。

デフォルトのインストール先は `~/.claude/commands/orch.md`。内容が同一なら何も変更しない。
別内容の既存コマンドは `--force` なしでは上書きせず、強制置換時も一意な名前のバックアップを
先に作成する。別の場所には `--target PATH` でインストールできる。

## ターミナルから使う

今回の利用に合わせて対話形式で割り当てを選ぶには、次を実行する:

```bash
uv run agentctl start
```

ウィザードはインストール済みエンジンを検出し、永続 Run と一回限りの Batch のどちらにするかを
尋ね、自動ルートを表示したうえで、開始前に kind ごとの割り当てを上書きできる。TTY 専用で、
Run を選んだ場合は空の Run を作成して `workflow_id` を表示する。最初のタスクは下記の
`run dispatch` で追加する。スクリプトや再現可能な操作には、以下の明示形式を使う。

後から作業を追加する場合は永続 Run を作る:

```bash
uv run agentctl run create --repo ~/dev/my-project \
  --route implement=grok \
  --fallback implement=codex,claude

# create の出力にある workflow_id を使う。例: run-0007
uv run agentctl run dispatch run-0007 --task "パーサーを追加して" --kind implement
uv run agentctl run show run-0007
uv run agentctl run list
uv run agentctl run close run-0007
```

独立した全タスクが分かっている場合は、一回限りの Batch で投入する。tasks file はタスク仕様の
JSON 配列で、`--tasks-file -` を指定すると標準入力から読み込む。

```json
[
  {
    "task": "パーサーを追加して",
    "kind": "implement",
    "risk": "normal",
    "priority": "high",
    "base_ref": "main"
  },
  {
    "task": "認証フローをレビューして",
    "kind": "review"
  }
]
```

タスクで使えるキーは `task`、`kind`、`risk`、`priority`、`parent_id`、`base_ref`。
エンジンはワークフロー側の `--route` で選び、Batch のタスクオブジェクトに `engine` は書けない。

```bash
uv run agentctl batch dispatch --repo ~/dev/my-project \
  --route implement=grok \
  --fallback implement=codex,claude \
  --tasks-file tasks.json
uv run agentctl batch list
```

割り当てる kind ごとに `--route KIND=ENGINE` を繰り返す。省略した kind は、ワークフロー作成時の
自動 primary を継承する。入力では `agy` を別名として受け付けるが、保存値と出力では常に
正規名 `antigravity` を使う。

フォールバックは、リストを指定したかどうかで挙動が明確に変わる:

- kind の `--fallback` を省略すると、その時点の自動ルート順を保存する。後の実行時にその
  候補がすべて利用不能なら、自動モードは別のインストール済みエンジンを決定的な順で選ぶ。
  primary を上書きした場合、保存する明示候補はその primary、作成時の設定済み primary と
  fallback（重複除去済み）の順になり、同時に保存する自動モードが最後の任意エンジン選択を許す。
- 空でない順序付きリストを指定すると、その順序だけを厳密かつ網羅的に使う。primary と
  リスト内のどのエンジンもインストールされていなければ、別のエンジンを暗黙に選ばず失敗する。
  同じ kind の `--route` が必要で、primary 自体や重複を含めることはできず、空リストも無効。

`run show` と `batch show` では、保存されたルート、fallback mode、各タスクで実際に選ばれた
エンジンを確認できる。

### 従来の単一タスクコマンド

元からある単一タスクコマンドも引き続き利用できる:

```bash
uv run agentctl add --repo ~/dev/my-project --task "READMEのセットアップ手順を最新化して"
uv run agentctl add --repo ~/dev/my-project --task "calc.py をレビューして" --kind review --risk read_only
uv run agentctl dispatch --repo ~/dev/my-project --task "パーサーをレビューして" --kind review --engine codex
uv run agentctl dispatch --repo ~/dev/my-project --task "..." --json  # add と開始を一発で。CAGE が呼ぶのはこれ

uv run agentd run-task task-0001      # 1 件実行
uv run agentd run --max-concurrency 2 # キューを消化

uv run agentctl list
uv run agentctl show task-0001
```

`add` と `dispatch` のオプション: `--kind`、`--engine`、`--risk`、`--priority`、`--parent`、`--base-ref`。
明示した `--engine` はそのタスクの kind による自動ルートより優先され、即時 dispatch では
そのエンジンが未インストールならタスク作成前に失敗する。

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
uv sync --extra api
uv run agentapi run            # 127.0.0.1:8765
cd ui && deno task dev
```

ワークフロー内のタスクが通常の flat task として表示されることはあるが、workflow ID、route
snapshot、Run の close、Batch の seal は管理しない。すべてのプロセスで同じ runtime root を
指定する。フロントエンドの詳細は [UI README](ui/README.md) を参照。

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
