# orch

[English](README.en.md) | 日本語

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

この表は固定割り当てではなくデフォルトである。対話式の `orch start`、単一タスクへの厳密な
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
uv sync                      # .venv/bin に orch を作る
uv tool install -e ".[mcp,api]"   # PATH に通す（-e なので編集は即反映）
orch engines                 # このマシンにあるエンジンとルーティングテーブルを表示
```

`uv tool install` を省く場合は `uv run orch engines` のように毎回書くか、
`.venv/bin` を PATH に入れる。

コマンドは `orch` ひとつに集約されている:

| | |
|---|---|
| `orch add` / `dispatch` / `list` / `show` | タスクの投入と確認 |
| `orch run` / `batch` / `start` | Run・Batch ワークフロー |
| `orch stats` / `usage` / `engines` | 集計と環境確認 |
| `orch daemon run` | キューを消化するワーカー |
| `orch api run` | ローカル HTTP API（UI 用） |
| `orch mcp` | MCP サーバー（stdio） |

旧名の `agentctl` / `agentd` / `agentapi` / `agentmcp` もエイリアスとして残っているため、
既存の登録やスクリプトはそのまま動く。

各 CLI のインストールと認証はそれぞれ個別に必要。このツールが認証情報を保存することはない。

### Claude Code から使う

MCP サーバーを登録し、リポジトリに含まれる `/orch` コマンドテンプレートをインストールする:

```bash
claude mcp add orch -s user -- orch mcp
orch install-claude-command --locale ja
```

`--locale ja` は、入力候補の説明と引数ヒントだけでなく、確認や最終報告も日本語化する。英語版を
使う場合は `--locale` を省略するか、`--locale en` を指定する。

新しい Claude Code セッションを開始し、`/orch <やってほしいこと>` と入力する。`/orch` は何も
実行する前に、利用可能なエンジン、Run/Batch の案、割り当てを提示し、確認を待つ。MCP ツールを
直接呼び出すこともできる。`/absolute/path/to/orch` はこの checkout の絶対パスに置き換える。
インストーラが配置するのはコマンドファイルだけで、MCP サーバーの登録は行わない。また、
コマンドは上記の登録名 `orch` を前提とする。

直接使うツールは、Run 用の `orch_run_create`・`orch_run_dispatch`・`orch_run_close`、Batch 用の
`orch_batch_dispatch`、再開・確認用の `orch_workflow_list`・`orch_workflow_show`。
個々のタスクは `orch_status`・`orch_wait`・`orch_result`・`orch_diff` で追跡し、成果物は
`orch_adopt` で worktree から取り出す。`orch_stats` は下記の集計（コスト、トークン、実行時間、
クォータ）をそのまま返し、`orch_usage` は各エンジンのアカウント残量と復活時刻を返す。ファンアウトや、あるエンジンに実装させて別のエンジンにレビューさせる
パターンは [Claude Code playbook](docs/CLAUDE-PLAYBOOK.md)を参照。

デフォルトのインストール先は `~/.claude/commands/orch.md`。内容が同一なら何も変更しない。
別内容の既存コマンドは `--force` なしでは上書きせず、強制置換時も一意な名前のバックアップを
先に作成する。別の場所には `--target PATH` でインストールできる。テンプレートの対応ロケールは
英語の `en`（デフォルト）と日本語の `ja`。

## ターミナルから使う

今回の利用に合わせて対話形式で割り当てを選ぶには、次を実行する:

```bash
orch start
```

ウィザードはインストール済みエンジンを検出し、永続 Run と一回限りの Batch のどちらにするかを
尋ね、自動ルートを表示したうえで、開始前に kind ごとの割り当てを上書きできる。TTY 専用で、
Run を選んだ場合は空の Run を作成して `workflow_id` を表示する。最初のタスクは下記の
`run dispatch` で追加する。スクリプトや再現可能な操作には、以下の明示形式を使う。

後から作業を追加する場合は永続 Run を作る:

```bash
orch run create --repo ~/dev/my-project \
  --route implement=grok \
  --fallback implement=codex,claude

# create の出力にある workflow_id を使う。例: run-0007
orch run dispatch run-0007 --task "パーサーを追加して" --kind implement
orch run show run-0007
orch run list
orch run close run-0007
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
orch batch dispatch --repo ~/dev/my-project \
  --route implement=grok \
  --fallback implement=codex,claude \
  --tasks-file tasks.json
orch batch list
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
orch add --repo ~/dev/my-project --task "READMEのセットアップ手順を最新化して"
orch add --repo ~/dev/my-project --task "calc.py をレビューして" --kind review --risk read_only
orch dispatch --repo ~/dev/my-project --task "パーサーをレビューして" --kind review --engine codex
orch dispatch --repo ~/dev/my-project --task "..." --json  # add と開始を一発で。CAGE が呼ぶのはこれ

orch daemon run-task task-0001      # 1 件実行
orch daemon run --max-concurrency 2 # キューを消化

orch list
orch show task-0001
orch list --json           # orch_list と同じ形
orch show task-0001 --json
```

`add` と `dispatch` のオプション: `--kind`、`--engine`、`--risk`、`--priority`、`--parent`、`--base-ref`。
明示した `--engine` はそのタスクの kind による自動ルートより優先され、即時 dispatch では
そのエンジンが未インストールならタスク作成前に失敗する。`--repo` はどちらのコマンドでも
存在確認され、`~` は展開される。

結果を返すコマンドはすべて `--json` を取る。人間向けの表は桁を揃えて出るので、機械で
読むときは `--json` を使うこと。`--runtime-root` はサブコマンドの前後どちらにも書ける
（両方書いた場合は後ろが勝つ）。使用例は `orch --help` の末尾にある。

## 実行コストの集計

完了したタスクは必ず自分の実績値を記録する。エンジンが報告する場合はコスト、正規化された
トークン数、エンジンの実行時間、動いたコード量。`orch stats` がそれを合計し、
`orch_stats` が同じ数値を Claude に返す。

```bash
orch stats                          # このマシンの全実行
orch stats --workflow run-0001      # Run / Batch 単位
orch stats --group-by engine        # エンジン別の内訳
orch stats --since 2026-08-01 --repo ~/dev/my-project
orch stats --json                   # 1 行の JSON
```

フィルタ: `--repo`、`--workflow`、`--engine`、`--kind`、`--since`、`--until`。
`--group-by` は `engine` / `model` / `kind` / `status` / `repo` を取る。`run show` と
`batch show` にも、そのワークフローのタスクだけを対象にした同じ合計が出る。

```
tasks: 5
by_status: needs_review=4, failed=1
success_rate: 80.0%
cost_usd: 0.3971 (1/5 terminal tasks reported; no cost from codex)
tokens: 2304070 (2/5 tasks reported; input=167635, output=39667, cache_read=2096768, ...)
engine_s_total: 908.5
engine_s_p50: 285.5
```

読み違えやすい点が 2 つある:

- **コスト合計は原理的に部分値**。コストを報告するのは grok と claude だけなので、
  数値は必ず「何件中何件が報告したか」と「どのエンジンが欠けているか」を伴って出る。
  どのエンジンが報告するかは `orch engines` で確認できる。
- **`needs_review` が成功の終状態**。自分の成果物を自分で完了扱いにするものはいないので、
  正常に終わった実行はここで止まり、completed として数えられる。`succeeded` は手動で
  付けたときにしか入らない。

`engine_s_*` はエンジンプロセスだけの時間で、worktree の作成やキュー待ちを含まない。
キュー待ちは `orch_status` が `queue_wait_s` として別に返す。

トークン数は 4 エンジン間で正規化してある。フィールド名がそれぞれ違ううえ、input に
キャッシュ読み込みを含むかどうかもエンジンによって割れているため、ここでの
`input_tokens` は常に非キャッシュ分を指す。エンジンごとの対応表と、その根拠にした
実測値は `docs/engine-capabilities.md` にある。

### モデルとクォータ

`--group-by model` で、実際に走ったモデル別に同じ数値を割れる。同じ種類のタスクを
違う reasoning effort で流したときに効く。grok と claude はモデル名を stdout に出すが、
codex は自分のセッションログにしか書かない。orch は既に保存している session id を
使ってそれを読み戻す。antigravity はどこにも出さないので `-` になる。

API 課金ではなく定額プランで動いているエンジンでは、ドルは単位として間違っている。
codex はプランと現在の窓の消費率を報告するので、価格を推定せずにそれをそのまま出す:

```
quota: codex 4.0% of a 7d window (plan=plus, resets 2026-08-18T00:47Z)
```

これは**アカウントのスナップショットであってタスク単位のコストではなく、合計もしない**。
`used_percent` はアカウント全体の値で整数に量子化されているため、同時に走った 2 本を
区別できず、短いタスクではそもそも動かない。タスク単位の消費量はトークン数で見る。

## エンジンごとの残量

`stats` が「このデータベースのタスクが何を使ったか」なら、`orch usage` は
「各サブスクリプションがあとどれだけ残っているか」を答える。orch の外での消費も含む
アカウント全体の値で、`orch_usage` が同じものを Claude に返す。

```bash
orch usage          # 4 エンジン分を一覧
orch usage --json   # 1 行の JSON
```

```
engine       installed  plan        window          used   resets_at          in      observed  detail
codex        yes        plus        primary (7d)    8.0%   2026-08-20T12:38Z  5d23h   53m ago   -
claude       yes        claude_pro  five_hour (5h)  1.0%   2026-08-11T17:40Z  elapsed 3d0h ago  -
claude       yes        claude_pro  seven_day (7d)  57.0%  2026-08-11T23:00Z  elapsed 3d0h ago  -
claude       yes        claude_pro  extra_usage     85.9%  -                  -       3d0h ago  8593/10000 credits, disabled (out_of_credits)
grok         yes        SuperGrok   credits (7d)    3.0%   2026-08-12T21:13Z  elapsed 2d16h ago -
antigravity  yes        -           -               -      -                  -       -         -

source: codex /Users/f42/.codex/sessions/2026/08/12/rollout-…jsonl
stale: claude a window reset after this reading, so real usage is lower than shown — …
note: antigravity agy reports no quota: its JSON result carries tokens only, …
```

どのエンジンにも残量を訊く API はない。取れるのは**各エンジンが自分で書いたファイル**
だけなので、`usage` はそれを読むだけでプロセスを起動しないし、認証ファイルにも触らない
（残量を見るコマンドが残量を消費しては本末転倒だし、トークンを読む理由もない）。
エンジンごとの取得元と実測の詳細は `docs/engine-capabilities.md` にある。

そのため、数字より重要なのが**いつ測られた値か**で、各行は必ず `observed`（測定時刻からの
経過）と取得元ファイルを伴って出る:

- **codex** は毎ターン記録するので、ほぼ常に新しい。
- **claude** は `/usage` のキャッシュで、Claude Code が動いている間しか更新されない。
  数日前の値であることが普通にある。
- **grok** は起動時に 1 回書く。
- **antigravity** は何も報告しない。省略せず「報告できない」と理由付きで並べる —
  一覧に出ないエンジンは、未インストールなのか報告できないのか区別がつかないため。

`in` が `elapsed` の行は、**その窓が測定後にリセット済み**という意味で、表示されている
消費率は既に戻っている。この場合は `stale:` 行が理由と更新方法を添えて出る。

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
MCP ツールまたは `orch` を使う。

```bash
uv sync --extra api
orch api run            # 127.0.0.1:8765
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
  ドル建ての合計は原理的に部分的な値になる。codex は代わりに定額プランとクォータを報告し、
  `stats` はそれを価格に換算せず別枠のスナップショットとして出す。
- クォータはアカウント全体の値で整数に量子化されているため、個別タスクへの帰属も合計もしない。
  タスク単位の消費量はトークン数で見る。
- antigravity はモデル名をどこにも報告しないため、それらのタスクは `-` にまとまる。
