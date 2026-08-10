---
description: orchワークフローでコーディング作業を計画し、他のエージェントへ委譲する
argument-hint: <依頼する作業>
allowed-tools:
  - mcp__orch__orch_engines
  - mcp__orch__orch_dispatch
  - mcp__orch__orch_run_create
  - mcp__orch__orch_run_dispatch
  - mcp__orch__orch_run_close
  - mcp__orch__orch_batch_dispatch
  - mcp__orch__orch_workflow_list
  - mcp__orch__orch_workflow_show
  - mcp__orch__orch_status
  - mcp__orch__orch_wait
  - mcp__orch__orch_result
  - mcp__orch__orch_diff
  - mcp__orch__orch_list
  - mcp__orch__orch_cancel
  - mcp__orch__orch_adopt
  - Read
  - Grep
  - Glob
  - Bash(git status:*)
  - Bash(git log:*)
  - Bash(git diff:*)
  - AskUserQuestion
---

次の依頼を `orch` でオーケストレーションする:

> $ARGUMENTS

ユーザーへの説明、確認、最終報告は日本語で行う。次のワークフローに従う:

1. 依頼内容と受け入れ条件を理解できる範囲までリポジトリを調査し、作業をタスクに分割する。依存関係を明示し、互いに依存するタスクや同じコードを編集しそうなタスクを並列実行しない。
2. 割り当てを提案する前に `orch_engines` を呼び出す。インストール済みエンジンの情報と自動ルーティングのデフォルトを使い、すべてのエンジンが利用可能だと仮定しない。
3. 何かをディスパッチする前に、次のすべてについてユーザーの明示的な確認を得る:
   - **Run または Batch**: 後からタスクを追加する可能性がある場合や、前段の結果をレビューしてから後段を決める場合は Run を使う。独立したタスク一式がすべて確定している場合に限り Batch を使う。
   - **ルート**: 使用する各タスク種別のプライマリエンジン。入力では `agy` を `antigravity` の別名として使用できるが、出力では正式名の `antigravity` を使う。
   - **フォールバック**: そのタスク種別のフォールバックリストを省略すると、スナップショットされた自動順序を使う。空でない順序付きリストを指定すると、厳密な手動順序として扱う。手動リストは網羅的であり、リスト外のエンジンへ切り替えない。
4. 確認後、承認されたワークフローだけを作成してディスパッチする:
   - Run の場合は `orch_run_create` を呼び出し、`orch_run_dispatch` で作業を追加する。各結果を待ってレビューしてから次の段階を決める。Run の所属情報と `parent_id` は、コミットされていない worktree の変更を引き継がない。後続タスクが先行タスクのコードを必要とする場合は、ユーザーが変更を承認・採用・コミットするまで停止し、そのコミットを `base_ref` として渡す。タスクを追加し終えたら `orch_run_close` を呼び出す。
   - Batch の場合は、独立した全タスクを揃えて `orch_batch_dispatch` を一度だけ呼び出す。依存関係のある段階を一つの Batch として表現しない。
   - 保存済みワークフローの再開や確認には `orch_workflow_list` と `orch_workflow_show` を使う。
5. 待機に入る前に、すべてのディスパッチ結果で `spawn_error` を確認する。spawn error があるタスクは永続化されているが、外部デーモン向けのキューに残っている。その旨を報告し、実際に開始するまでは `orch_wait` の対象に含めない。その他のディスパッチ済みタスクは `orch_wait` で合流し、個別の進捗確認が必要な場合だけ `orch_status` を使う。完了したすべてのタスクについて `orch_result` と `orch_diff` を読み、採用を勧める前に失敗、警告、競合を報告する。
6. レビュー中は読み取り専用のパッチ戦略で `orch_adopt` を呼び出す。実リポジトリへの変更適用は、レビュー済みパッチをユーザーが明示的に承認した後に限る。ユーザーに代わってコミット、push、デプロイを行わない。

最後に、確認済みルート、タスクの結果、レビューした変更、人間の判断が残っている事項を簡潔に報告する。
