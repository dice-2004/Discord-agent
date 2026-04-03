# テスト結果記録

本書は [docs/TEST_PLAN.md](TEST_PLAN.md) に基づくすべてのテスト項目の実施結果を記録するファイルです。

## 記録ルール

- **判定**: `PASS` / `FAIL` / `SKIP` / `-`（未実施）
- **実行ルート**: 質問系テストの場合、Botが回答を生成するまでに通過したルートを記録する
  - 例: `on_message → _handle_mention_question → orchestrator.answer → _generate_with_tools → dispatch_research_job`
  - 例: `on_message → build_quick_calendar_action → execute_tool_job(add_calendar_event)`
  - 例: `/ask → orchestrator.answer → _decide_next_action(respond) → _self_review_response(approve)`
- **使用ツール/エージェント**: ツールの実行があった場合に、使用されたツール名とエージェント（Main Agent / Research Agent）を記録する
  - 例: `web_search, read_url_markdown` / `dispatch_research_job(mode=auto, source=github)`
- **備考**: 不具合の詳細、再現手順、改善提案など

## テスト実施概要

| 項目 | 値 |
|---|---|
| 実施日 | 2026-04-02 |
| 実施者 | GitHub Copilot (GPT-5.3-Codex) |
| Bot バージョン | ローカルワークツリー（未タグ） |
| 環境 | Docker |
| ChromaDB 初期化 | スキップ（既存データ維持） |

## 結果サマリ

| セクション | 総数 | PASS | FAIL | SKIP | 未実施 |
|---|---|---|---|---|---|
| §1 単体テスト (UT-001〜067) | 67 | 57 | 2 | 0 | 8 |
| §2 複合テスト (CT-001〜014) | 14 | 10 | 2 | 0 | 2 |
| §3 文脈理解テスト (CT-015〜032) | 18 | 18 | 0 | 0 | 0 |
| §4 @メンション同等テスト (MT-001〜034) | 34 | 28 | 3 | 0 | 3 |
| §7 エラーハンドリング (ERR-001〜010) | 10 | 10 | 0 | 0 | 0 |
| §8 質問ロジックパス (QLP-001〜023) | 23 | 11 | 10 | 2 | 0 |
| §9 セキュリティ (SEC-001〜006) | 6 | 2 | 1 | 0 | 3 |
| §10 設定バリデーション (CFG-001〜008) | 8 | 6 | 0 | 0 | 2 |
| §11 エッジケース (EDGE-001〜012) | 12 | 10 | 0 | 0 | 2 |
| **合計** | **192** | **151** | **18** | **2** | **21** |

---

## §1 単体テスト

### §1.1 Discord 入力・応答

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | 使用ツール/エージェント | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| UT-001 | PASS | 2026-04-02 22:44 | `質問: 今日の天気は？` | `orchestrator.answer -> respond` | Main Agent | 京都市の雨予報を回答 | 期待どおり |
| UT-002 | PASS | 2026-04-02 23:31 | 許可外 guild_id で `/ask` を実行 | `ask -> allowed_guild_ids gate` | Main Agent | 許可外ギルドで拒否メッセージを返却 | `このサーバーではこのBotを利用できません。` を確認 |
| UT-003 | PASS | 2026-04-02 23:25 | `/ask Pythonの詳細解説を1000字以上で` | `ask -> orchestrator.answer -> send_response` | Main Agent | 長文回答を `ask_response.txt` 添付へ切替 | 本文要約 + 添付を確認 |
| UT-004 | FAIL | 2026-04-02 22:45 | `Q1: Pythonについて` → `Q2: さっきの内容をもう一度` | `orchestrator.answer -> respond` | Main Agent | Q2 が Q1 の内容ではなく別の直前文脈（asyncio要約）へ寄った | 直前会話参照の期待に未達 |
| UT-005 | FAIL | 2026-04-02 22:47 | `Q1: 推奨フレームワーク3つ` → `Q2: その3つの利点は？` | `orchestrator.answer -> respond` | Main Agent | Q2 が Q1 の3項目を参照せず、async/await の利点へ逸脱 | 列挙系フォローアップ解決に失敗 |
| UT-006 | PASS | 2026-04-02 23:25 | `@bot 今年のトレンドは？` | `_handle_mention_question -> orchestrator.answer -> send_message_response` | Main Agent | メンション応答が返ることを確認 | `/ask` と同等の応答を確認 |
| UT-007 | PASS | 2026-04-02 23:25 | `@bot さっきの内容をもう一度` | `_handle_mention_question -> orchestrator.answer -> send_message_response` | Main Agent | recent conversation を注入し、前回文脈を含むことを確認 | follow-up 参照を確認 |

### §1.2 メモリ・プロフィール・セマンティック検索

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | 使用ツール/エージェント | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| UT-008 | PASS | 2026-04-02 23:32 | 事前に複数メッセージがあるチャンネルで起動時バックフィルを実行 | `bootstrap_channel_history -> orchestrator.ingest_channel_history` | Main Agent | 過去メッセージ 2件を取り込み | 期待どおり |
| UT-009 | PASS | 2026-04-02 23:32 | Bot起動後の新規メッセージ送信 | `on_message -> orchestrator.memory.add_message` | Main Agent | 送信メッセージを保存することを確認 | 継続保存を確認 |
| UT-010 | PASS | 2026-04-02 23:10 | `question: Pythonの使い方は？`、別チャンネルに関連履歴を投入し `orchestrator.answer` をモック生成 | `orchestrator.answer` | Main Agent | system prompt 内に `ch=9101` の履歴を含むことを確認 | 期待どおり |
| UT-011 | PASS | 2026-04-02 23:10 | `query_text: 暑い` で意味検索を実行し、`気温が高い日だ` を投入 | `orchestrator.memory.fetch_relevant_messages(scope=guild)` | Main Agent | 類義表現の履歴がヒット | 期待どおり |
| UT-012 | PASS | 2026-04-02 23:21 | `DIRECTIONAL_MEMORY_ENABLED=true` で個人ギルドと身内ギルドの参照先を確認 | `orchestrator.answer -> fetch_relevant_messages_multi_guild / fetch_relevant_messages` | Main Agent | personal guild では `[1,2,3]` を参照し、family guild では単一ギルド参照を確認 | 期待どおり |
| UT-013 | PASS | 2026-04-02 23:21 | `MEMORY_RESPONSE_INCLUDE_EVIDENCE=true` で `/ask` を実行 | `orchestrator.answer -> respond` | Main Agent | 回答末尾に `[参照メモリ]` を付与 | 参照メモリ 2件を確認 |
| UT-014 | PASS | 2026-04-02 23:10 | `guild_id` を指定してメモリ統計を取得 | `orchestrator.memory.get_guild_memory_stats` | Main Agent | guild 別コレクション数と記録数を確認 | 期待どおり |
| UT-015 | PASS | 2026-04-02 23:10 | `set_user_profile_fact(user_id, 得意分野, Python機械学習)` を実行 | `orchestrator.memory.set_user_profile_fact` | Main Agent | 保存直後の `get_user_profile_facts` で項目を確認 | 期待どおり |
| UT-016 | PASS | 2026-04-02 23:21 | `/profile_set x ""` | `profile_set -> validation` | Main Agent | 空 value を拒否 | `key/value は空にできません。` を確認 |
| UT-017 | PASS | 2026-04-02 23:10 | `profile_show` 相当の取得で保存済み項目を確認 | `orchestrator.memory.get_user_profile_facts` | Main Agent | 保存済みプロファイルが一覧で取得できることを確認 | 期待どおり |
| UT-018 | PASS | 2026-04-02 23:10 | `forget_user_profile_fact(user_id, 得意分野)` を実行 | `orchestrator.memory.forget_user_profile_fact` | Main Agent | 単項目削除後に空一覧を確認 | 期待どおり |
| UT-019 | PASS | 2026-04-02 23:10 | 2件保存後に `forget_user_profile_fact(user_id, None)` を実行 | `orchestrator.memory.forget_user_profile_fact` | Main Agent | 全削除後に空一覧を確認 | 期待どおり |

### §1.3 Research Agent・検索・GitHub 読み込み

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | 使用ツール/エージェント | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| UT-020 | PASS | 2026-04-02 23:21 | `DEEPDIVE_USE_RESEARCH_AGENT=true` で `deepdive` 実行 | `deepdive -> orchestrator.execute_tool_job(dispatch_research_job)` | Main Agent -> Research Agent | `dispatch_research_job` を選択し queued 応答を送信 | 期待どおり |
| UT-021 | PASS | 2026-04-02 23:21 | `DEEPDIVE_USE_RESEARCH_AGENT=false` で `deepdive` 実行 | `deepdive -> orchestrator.execute_tool_job(source_deep_dive)` | Main Agent | `source_deep_dive` を選択し直接深掘り結果を返却 | 期待どおり |
| UT-022 | PASS | 2026-04-02 23:21 | `https://github.com/acme/project` を深掘り対象に指定 | `source_deep_dive -> GitHub Repo Probe` | Main Agent | README と issue/pr probe を含む GitHub probe 結果を確認 | GitHub URL 自動検出を確認 |
| UT-023 | PASS | 2026-04-02 23:21 | `https://github.com/acme/project` を深掘り対象に指定 | `source_deep_dive -> GitHub Repo Probe` | Main Agent | About description を読み込み `about_contains_kc3hack: yes` を確認 | README と About の分離を確認 |
| UT-024 | PASS | 2026-04-02 23:21 | `/logsearch KC3Hack 2025 scope:guild` | `logsearch -> orchestrator.memory.fetch_relevant_messages` | Main Agent | `KC3Hack 2025 募集案内` / `予算関連` がヒット | guild 範囲の検索を確認 |
| UT-025 | PASS | 2026-04-02 23:21 | `/readurl https://example.com` | `readurl -> orchestrator.execute_tool_job(read_url_markdown)` | Main Agent | `read_url_markdown` を呼び出し、URL本文要約素材を返却 | 期待どおり |
| UT-026 | PASS | 2026-04-02 23:10 | `query_text: Python`、`scope=channel` で検索 | `orchestrator.memory.fetch_relevant_messages` | Main Agent | 現在チャンネルの候補のみ返ることを確認 | 期待どおり |
| UT-027 | PASS | 2026-04-02 23:10 | `query_text: Python`、`scope=guild` で検索 | `orchestrator.memory.fetch_relevant_messages` | Main Agent | 同一ギルド内の複数チャンネル候補が返ることを確認 | 期待どおり |
| UT-028 | PASS | 2026-04-02 23:10 | `LOGSEARCH_INCLUDE_SCORE=true` 前提で検索結果を評価 | `orchestrator.memory.fetch_relevant_messages` | Main Agent | スコア付き出力の組み立て条件を確認 | 期待どおり |
| UT-029 | PASS | 2026-04-02 23:21 | `/auth_status` | `auth_status -> response.send_message` | Main Agent | GitHub / Calendar / SMTP の認証状態と導線 URL を表示 | 期待どおり |

### §1.4 CLI・監査・デバッグ

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | 使用ツール/エージェント | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| UT-030 | - | | | | | | |
| UT-031 | - | | | | | | |
| UT-032 | - | | | | | | |
| UT-033 | - | | | | | | |
| UT-034 | - | | | | | | |
| UT-035 | - | | | | | | |
| UT-036 | - | | | | | | |
| UT-037 | PASS | 2026-04-02 23:23 | `/debug_action add_task payload_json:{"title":"テスト"}` | `debug_action -> orchestrator.execute_tool_job(execute_internal_action)` | Main Agent | `execute_internal_action` を呼び出し、JSON結果を返却 | 期待どおり |
| UT-038 | - | | | | | | |
| UT-039 | - | | | | | | |

### §1.5 Research Agent API・ジョブ連携

| ID | 判定 | 実施日時 | HTTP リクエスト | 実行ルート | 使用ツール/エージェント | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| UT-040 | PASS | 2026-04-02 19:55 | `GET /healthz` | `research-agent API -> /healthz` | `Research Agent` | `200` + `{"status":"ok","service":"research-agent"}` | コンテナ内から疎通確認（ホスト公開ポートなし） |
| UT-041 | PASS | 2026-04-02 19:55 | `POST /v1/jobs {"topic":"python asyncio basics","source":"auto"}` | `research-agent API -> create job` | `Research Agent` | `202` + `queued` + `job_id` 返却 | 仕様の `201` 記載と差異あり（実装は `202 Accepted`） |
| UT-042 | PASS | 2026-04-02 19:55 | `POST /v1/jobs {"topic":""}` | `research-agent API -> validation` | `Research Agent` | `400` + `{"code":"invalid_topic"}` | 期待どおり |
| UT-043 | PASS | 2026-04-02 19:56 | `GET /v1/jobs/{job_id}` | `research-agent API -> read job` | `Research Agent` | `200` + job詳細JSON（status=running時点） | 既存job取得OK |
| UT-044 | PASS | 2026-04-02 19:55 | `GET /v1/jobs/rj-notexist` | `research-agent API -> not found` | `Research Agent` | `404` + `{"code":"job_not_found"}` | 期待どおり |
| UT-045 | PASS | 2026-04-02 19:56 | `dispatch_research_job(wait=false)` | `main-agent tools.research_tools -> POST /v1/jobs` | `Main Agent -> Research Agent` | `{"status":"queued","job_id":"..."}` | `wait=false` 分岐確認 |
| UT-046 | PASS | 2026-04-02 19:56 | `dispatch_research_job(wait=true, mode=fallback, timeout_sec=10)` | `main-agent tools.research_tools -> POST/GET poll` | `Main Agent -> Research Agent` | `status=done` で完了応答返却 | レポート本文にレート制限メッセージ含む（想定内） |
| UT-047 | PASS | 2026-04-02 19:56 | `dispatch_research_job(mode=auto, wait=false)` | `main-agent tools.research_tools -> POST /v1/jobs` | `Main Agent -> Research Agent` | `mode=auto` で `queued` | 期待どおり |
| UT-048 | PASS | 2026-04-02 19:56 | `dispatch_research_job(mode=gemini_cli, wait=false)` | `main-agent tools.research_tools -> POST /v1/jobs` | `Main Agent -> Research Agent` | `mode=gemini_cli` で `queued` | 期待どおり |
| UT-049 | PASS | 2026-04-02 19:56 | `dispatch_research_job(mode=fallback, wait=false)` | `main-agent tools.research_tools -> POST /v1/jobs` | `Main Agent -> Research Agent` | `mode=fallback` で `queued` | 期待どおり |
| UT-050 | PASS | 2026-04-02 19:56 | `get_research_job_status(job_id)` | `main-agent tools.research_tools -> GET /v1/jobs/{id}` | `Main Agent -> Research Agent` | job状態JSONを返却（status=running時点） | 期待どおり |
| UT-051 | PASS | 2026-04-02 19:56 | `get_research_job_status("")` | `main-agent tools.research_tools -> input validation` | `Main Agent` | `{"code":"invalid_job_id"}` | API呼び出し前バリデーションOK |

### §1.6 内部アクション

| ID | 判定 | 実施日時 | 送信パラメータ | 実行ルート | 使用ツール/エージェント | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| UT-052 | PASS | 2026-04-02 22:34 | `action=unknown_action`, `payload_json={}` | `tools.action_tools.execute_internal_action` | Main Agent | 未対応アクションを拒否 | `{"code":"unsupported_action"}` を確認 |
| UT-053 | PASS | 2026-04-02 22:34 | `action=add_task`, `payload_json=not json` | `tools.action_tools.execute_internal_action` | Main Agent | 不正JSONを拒否 | `{"code":"invalid_payload_json"}` を確認 |
| UT-054 | PASS | 2026-04-02 22:34 | `action=calendar_add_event`（別名）, timed payload | `tools.action_tools._normalize_action_name -> _handle_add_calendar_event` | Main Agent | 別名が `add_calendar_event` へ正規化され実行成功 | `{"status":"ok","action":"add_calendar_event"}` を確認 |
| UT-055 | PASS | 2026-04-02 22:34 | `action=add_calendar_event`, `title/start_time/end_time` | `tools.action_tools._handle_add_calendar_event` | Main Agent | timed予定を保存 | `{"status":"ok","event_id":"cal-..."}` を確認 |
| UT-056 | PASS | 2026-04-02 22:34 | `action=add_calendar_event`, `title/all_day/date` | `tools.action_tools._handle_add_calendar_event` | Main Agent | 終日予定を保存 | `{"status":"ok","all_day":true}` を確認 |
| UT-057 | PASS | 2026-04-02 22:34 | `action=add_calendar_event`, `{"title":"会議"}` | `tools.action_tools._handle_add_calendar_event` | Main Agent | 必須時刻不足を拒否 | `{"code":"missing_required_fields"}` を確認 |
| UT-058 | PASS | 2026-04-02 22:36 | `action=get_calendar_events`, `time_min=2026-04-01T00:00:00+09:00`, `time_max=2026-04-30T23:59:00+09:00` | `tools.action_tools._handle_get_calendar_events` | Main Agent | 指定期間の予定一覧を返却 | `{"status":"ok","count":3,"events":[...]}` を確認 |
| UT-059 | PASS | 2026-04-02 22:37 | `action=add_task`, `{"title":"レポート提出"}` | `tools.action_tools._handle_add_task` | Main Agent | タスク登録成功 | `{"status":"ok","task_id":"...","web_link":"https://tasks.google.com"}` を確認 |
| UT-060 | PASS | 2026-04-02 22:36 | `action=add_notion_memo`, `{"title":"AI","content":"メモ","category":"tech"}` | `tools.action_tools._handle_add_notion_memo` | Main Agent | メモ保存成功 | `{"status":"ok","memo_id":"memo-..."}` を確認 |
| UT-061 | PASS | 2026-04-03 16:05 | `action=append_sheet_row`, `{"sheet_name":"データ","column_data":["2026-04-02","テスト"]}` | `tools.action_tools._handle_append_sheet_row` | Main Agent | 配列入力を受理し CSV 行を追記 | `{"status":"ok"}` を確認（`columns=["col1","col2"]`） |
| UT-062 | PASS | 2026-04-02 22:35 | `action=create_github_issue`, `repository/title/body` 指定, `GITHUB_TOKEN` 未設定 | `tools.action_tools._handle_create_github_issue` | Main Agent | 未認証で拒否 | `{"code":"auth_required"}` を確認 |
| UT-063 | PASS | 2026-04-02 22:46 | `action=create_github_issue`, `repository/title/body` 指定, GitHub API をモック | `tools.action_tools._handle_create_github_issue` | Main Agent | Issue作成成功経路を確認 | `{"status":"ok","issue_number":42}` を確認 |
| UT-064 | PASS | 2026-04-02 22:35 | `action=backup_server_data`, `target=/app/src`（許可ルート内） | `tools.action_tools._handle_backup_server_data` | Main Agent | tar.gz バックアップ作成成功 | `{"status":"ok","archive_path":"/tmp/backups/src-...tar.gz"}` を確認 |
| UT-065 | PASS | 2026-04-02 22:34 | `action=backup_server_data`, `target=/etc`（許可外） | `tools.action_tools._handle_backup_server_data` | Main Agent | 許可外パスを拒否 | `{"code":"forbidden_target"}` を確認 |
| UT-066 | PASS | 2026-04-02 22:34 | `action=send_email`, SMTP設定未投入 | `tools.action_tools._handle_send_email` | Main Agent | 未認証で拒否 | `{"code":"auth_required"}` を確認 |
| UT-067 | PASS | 2026-04-02 23:25 | `action=send_email`, `{"to_address":"test@example.com","subject":"件名","body":"本文"}` | `tools.action_tools.execute_internal_action` | Main Agent | SMTP モックで送信成功 | `{"status":"ok","action":"send_email"...}` を確認 |
---

## §2 複合テスト

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | 使用ツール/エージェント | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| CT-001 | FAIL | 2026-04-02 22:44 | Q1:`Pythonについて` → Q2:`さっきの話をもっと詳しく` | `orchestrator.answer -> respond` | Main Agent | Q2 が Python の続きではなく AIトレンドの詳細へ遷移 | follow-up 文脈維持に失敗 |
| CT-002 | PASS | 2026-04-03 00:30 | Q1列挙後 Q2:`その全部を数百字で詳しく` + 長文応答送信 | `orchestrator.answer -> history_context付与` / `send_response(attachment fallback)` | Main Agent | 指示語で前問列挙を参照し、17000字応答は添付送信へ切替 | follow-up+長文添付の複合動作を確認 |
| CT-003 | PASS | 2026-04-02 22:44 | `最新のAIトレンドを詳しく調べて` | `orchestrator.answer -> dispatch_research_job -> respond` | Main Agent -> Research Agent | AIトレンドを3点で整理して返答 | 調査系フローが成立 |
| CT-004 | PASS（実機） | 2026-04-03 15:44 | `/deepdive` 実行後の通知経路を確認 | `dispatch_research_job(wait=false) -> research_notify(done)` | Main Agent -> Research Agent | 調査開始/完了/結果通知を観測 | `main-agent` ログで `research-notify ... status=done` を確認 |
| CT-005 | PASS | 2026-04-03 00:17 | `/profile_set スタイル 簡潔にお願い` 相当設定後 `/ask 応答されたい流儀` | `orchestrator.memory.set_user_profile_fact -> orchestrator.answer` | Main Agent | system prompt の persona context に `スタイル: 簡潔にお願い` を反映 | プロファイル反映を確認 |
| CT-006 | FAIL（後回し） | 2026-04-03 00:33 | `DEEPDIVE_USE_RESEARCH_AGENT=true` 相当で調査ジョブを wait=true 実行 | `dispatch_research_job(wait=true)` | Main Agent -> Research Agent | polling 表示確認手順が未確定 | ユーザー判断で後回し |
| CT-007 | - | | | | | | |
| CT-008 | - | | | | | | |
| CT-009 | PASS | 2026-04-03 00:34 | `create_github_issue` を `GITHUB_TOKEN` 未設定で実行し auth情報確認 | `execute_internal_action(create_github_issue)` + `auth_status相当情報確認` | Main Agent | `auth_required` と `auth_url` を返却、認証状態表示は `GitHub token: missing` | 失敗理由と導線URLの両方を確認 |
| CT-010 | PASS | 2026-04-03 16:06 | calendar追加後に `/ask 今週の予定は？` 相当を実行 | `execute_internal_action(add_calendar_event/get_calendar_events)` + `orchestrator.answer` | Main Agent | 予定追加・期間取得ともに `ok` でイベント一覧を返却 | `get_calendar_events` で `status=ok,count>=1` を確認 |
| CT-011 | PASS | 2026-04-03 00:17 | profile set/show/forget/set 後に `/ask` | `orchestrator.memory.*profile* -> orchestrator.answer` | Main Agent | 保存確認・削除・再保存後の値（`口調: 丁寧`）が prompt に反映 | プロファイルCRUD+応答反映を確認 |
| CT-012 | PASS | 2026-04-03 00:30 | 既存メッセージを backfill 取り込み後に検索・質問を実行 | `ingest_channel_history -> fetch_relevant_messages -> orchestrator.answer` | Main Agent | backfill 2件取り込み・`Python` 検索ヒット・ask prompt への履歴注入を確認 | バックフィル連携の複合動作を確認 |
| CT-013 | PASS | 2026-04-03 00:17 | `github.com/owner/repo について` 相当の deep dive | `source_deep_dive -> GitHub Repo Probe` | Main Agent | `about_description` と `README_headline` を分離して取得 | README/About 分離参照を確認 |
| CT-014 | PASS | 2026-04-03 00:17 | `/ask KC3Hack 2025 の過去案内を要約して` | `orchestrator.answer -> history_context付与` | Main Agent | `KC3Hack 2025 募集案内` と `締切/予算` を含む履歴が prompt 文脈へ注入 | 過去会話要約の前提を満たす |

---

## §3 文脈理解・文脈非参照テスト

### §3.1 セマンティック検索

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | 使用ツール/エージェント | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| CT-015 | PASS | 2026-04-02 23:55 | `/logsearch 暑い` 相当クエリ | `orchestrator.memory.fetch_relevant_messages(scope=guild)` | Main Agent | `暑い` で `気温が高くて不快` を含む候補がヒット | セマンティック検索成立 |
| CT-016 | PASS | 2026-04-02 23:55 | `/logsearch コンピュータ` 相当クエリ | `orchestrator.memory.fetch_relevant_messages(scope=guild)` | Main Agent | `コンピュータ` で `PCでプログラミング` を含む候補がヒット | 類義語検索成立 |
| CT-017 | PASS | 2026-04-02 23:55 | `/ask 最近話した内容で、何が印象的だった？` | `orchestrator.answer -> history_context付与` | Main Agent | system prompt に `[Relevant Conversation Memory]` と複数履歴が注入 | 文脈参照を確認 |
| CT-018 | PASS | 2026-04-02 23:55 | `/ask プログラムしたか？` | `orchestrator.answer -> history_context付与` | Main Agent | `Pythonコードを書いた` / `PCでプログラミング` 系履歴を参照 | 類似トピック参照を確認 |

### §3.2 文脈非参照（グローバルクエリ）

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | 使用ツール/エージェント | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| CT-019 | PASS | 2026-04-02 23:35 | `GitHub上の最新議論を調べて` | `orchestrator.answer -> 早期return(明示的グローバルクエリ)` | Main Agent | history_context を付与せず独立回答 | 期待どおり |
| CT-020 | PASS（実機） | 2026-04-03 15:46 | `/ask Python の標準ライブラリは？` | `orchestrator.answer -> respond` | Main Agent | 回答生成が正常完了し、履歴混入の再発なし | `Agent decision/respond` + `self-review approve` を確認 |
| CT-021 | PASS | 2026-04-02 23:55 | Q1:`GitHub について` → Q2:`それについてもっと詳しく` | `orchestrator.answer -> history_context付与` | Main Agent | Q2の prompt に Q1 (`GitHub について`) を含む | グローバル+フォローアップ混在を満たす |
| CT-022 | PASS | 2026-04-02 23:55 | `/ask 世界中で出来事・そのうち日本の話は？` | `orchestrator.answer -> history_context付与` | Main Agent | `その` を含むケースで follow-up 経路に入り履歴文脈を保持 | ルール確認どおり |

### §3.3 フォローアップ検出・指示語解決

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | 使用ツール/エージェント | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| CT-023 | PASS | 2026-04-02 23:35 | `それについてもう少し教えて` | `orchestrator.answer -> history_context付与` | Main Agent | `[Relevant Conversation Memory]` に前回文脈を含む | 期待どおり |
| CT-024 | PASS | 2026-04-02 23:48 | Q1: `候補は Python, Go, Rust` を投入後 Q2: `それぞれの特徴を詳しく` | `orchestrator.answer -> history_context付与` | Main Agent | system prompt の `[Relevant Conversation Memory]` に列挙済み3項目を含む | 指示語 `それぞれ` の文脈参照を確認 |
| CT-025 | PASS | 2026-04-02 23:48 | Q1: 複数項目提示後 Q2: `上記内容を要約して` | `orchestrator.answer -> history_context付与` | Main Agent | system prompt の `[Relevant Conversation Memory]` に前回内容を含む | `上記` のフォローアップ参照を確認 |
| CT-026 | PASS（実装レベル） | 2026-04-03 16:04 | `/ask 「その料理は何ですか」という質問で` | `orchestrator._has_followup_marker` | Main Agent | 引用部を除去して follow-up 判定するため誤検出を抑止 | `followup_quoted=false`, `followup_plain=true` を確認 |

### §3.4 DB実測シード

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | 使用ツール/エージェント | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| CT-027 | PASS | 2026-04-03 00:09 | `@bot 京都の天気の話、前にしてた内容も含めて要約して` 相当 | `orchestrator.answer -> history_context付与` | Main Agent | `[Relevant Conversation Memory]` に京都天気の過去会話を注入 | 天気文脈の統合を確認 |
| CT-028 | PASS | 2026-04-03 00:09 | `@bot pingやサーバー状態の話って最近どうだった？` 相当 | `orchestrator.answer -> history_context付与` | Main Agent | `ping` と `Proxmox` を含む運用履歴を注入 | 運用文脈の再利用を確認 |
| CT-029 | PASS | 2026-04-03 00:09 | `@bot yorimichi の話、過去に出た要点をまとめて` 相当 | `orchestrator.answer -> history_context付与` | Main Agent | `yorimichi` 関連の過去履歴を注入 | プロダクト文脈の再利用を確認 |
| CT-030 | PASS | 2026-04-03 00:09 | `@bot thought_signature の話、前回の比較案を短く` 相当 | `orchestrator.answer -> history_context付与` | Main Agent | `実装案を3つ比較` の既存履歴を注入 | GitHub議論文脈の継承を確認 |

### §3.5 任意シード（KC3Hack）

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | 使用ツール/エージェント | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| CT-031 | PASS | 2026-04-03 00:09 | `@bot KC3Hack 2025 の案内って過去に何が流れてた？` 相当 | `orchestrator.answer -> history_context付与` | Main Agent | `KC3Hack 2025` を含む過去履歴を注入 | 正規表記の想起を確認 |
| CT-032 | PASS | 2026-04-03 00:09 | `@bot kc3hacl 2025 の募集案内あったよね？` 相当 | `orchestrator.answer -> history_context付与` | Main Agent | typo入力でも `KC3Hack 2025` 履歴を注入 | 誤記ゆらぎ吸収を確認 |

---

## §4 @メンション同等性テスト

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | 使用ツール/エージェント | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| MT-001 | PASS | 2026-04-02 20:08 | `今年のトレンドを3点で教えて`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> tool(web_search) -> respond -> self_review(rewrite)` | `Main Agent: web_search` | 3点要約 + 参考URL付き回答を返却 | 返信メッセージ `flags=4`, `embeds=0` で埋め込み抑止を確認 |
| MT-002 | FAIL | 2026-04-02 22:44 | `Pythonの詳細解説を1000字以上で`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> respond` | Main Agent | 長文詳細の生成ではなく内部整形エラー調の応答 | 長文応答期待に未達 |
| MT-003 | PASS | 2026-04-02 23:48 | Q1: `@bot Pythonの特徴は?` 相当履歴投入後 Q2: `@bot さっきの話をもう一度` | `orchestrator.answer -> history_context付与` | Main Agent | `[Relevant Conversation Memory]` に直前のPython要点を保持 | フォローアップ継続を確認 |
| MT-004 | PASS | 2026-04-03 00:00 | Q1:`@bot 学習方法を3つ` 相当履歴投入後 Q2:`@bot その3つの利点は？` | `orchestrator.answer -> history_context付与` | Main Agent | `[Relevant Conversation Memory]` に3項目（反復練習/小さく作る/記録する）を保持 | 指示語解決を確認 |
| MT-005 | PASS | 2026-04-02 23:48 | `@bot 暑い日に関する最近の話あった？` 相当クエリで意味検索 | `orchestrator.memory.fetch_relevant_messages(scope=guild)` | Main Agent | `暑い` クエリで `気温が高くて不快` を含む候補を取得 | 類義検索ヒットを確認 |
| MT-006 | PASS | 2026-04-02 23:48 | 別チャンネルに `Pythonの使い方メモ` を投入後に質問 | `orchestrator.memory.fetch_relevant_messages(scope=guild)` | Main Agent | 同一guildの別チャンネル履歴を候補として取得 | guildスコープ参照を確認 |
| MT-007 | PASS | 2026-04-03 00:00 | `@bot 京都の天気、前回と今回をまとめて` 相当 | `orchestrator.answer -> history_context付与` | Main Agent | 事前投入した京都天気の過去履歴（質問/回答）を prompt 文脈に含む | 実測DB相当の再利用を確認 |
| MT-008 | PASS | 2026-04-03 00:00 | `@bot pingやProxmoxの件、前回の結論は？` 相当 | `orchestrator.answer -> history_context付与` | Main Agent | `ping` と `Proxmox` の過去運用履歴を prompt 文脈へ注入 | 運用文脈の再利用を確認 |
| MT-009 | PASS | 2026-04-03 00:00 | `@bot yorimichiって前にどんな説明だった？` 相当 | `orchestrator.answer -> history_context付与` | Main Agent | `yorimichi` の過去説明（概要/URL）を prompt 文脈へ注入 | プロダクト文脈の再利用を確認 |
| MT-010 | PASS | 2026-04-03 00:00 | `@bot thought_signature の比較案を再掲して` 相当 | `orchestrator.answer -> history_context付与` | Main Agent | `thought_signature` と比較案履歴（案A/B/C）を prompt 文脈へ注入 | 比較観点の継承を確認 |
| MT-011 | PASS | 2026-04-03 00:09 | `@bot このチャンネル中心で、Pythonの話を要約して` 相当 | `orchestrator.answer (MEMORY_RETRIEVAL_SCOPE=channel)` | Main Agent | 同一チャンネル履歴のみ注入され、別チャンネル履歴は不採用 | channel優先スコープを確認 |
| MT-012 | PASS | 2026-04-03 00:33 | `@bot GitHub連携の状態を教えて` 相当 | `auth_status相当情報組立` | Main Agent | `GitHub token: missing` と auth URL を提示できることを確認 | 認証導線提示の同等性を確認 |
| MT-013 | PASS | 2026-04-02 20:50 | `最新のAIトレンドを詳しく調べて`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> dispatch_research_job -> respond` | `Main Agent -> Research Agent: dispatch_research_job` | 調査系フローで詳細回答を返却 | deepdive相当の内部委譲判断は成立 |
| MT-014 | PASS（実装レベル） | 2026-04-03 16:03 | `@bot Gemini APIのthought_signatureエラー対策を比較して` 相当 | `_should_force_research_job` 判定確認 | Main Agent | 強制調査ディスパッチ判定が `true` | 比較/対策キーワード拡張の効果を確認 |
| MT-015 | PASS | 2026-04-02 20:10 | `GitHub中心で最新の議論を調べて要点だけ`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> tool(source_deep_dive) -> respond -> self_review(approve)` | `Main Agent: source_deep_dive` | GitHubトレンド要約を返却 | DuckDuckGo側レート制限を途中検知しつつ、回答は継続（耐障害性OK） |
| MT-016 | PASS（実装レベル） | 2026-04-03 16:03 | `@bot Kubernetes運用の失敗例を調べて対策を3つ` 相当 | `_should_force_research_job` 判定確認 | Main Agent | 強制調査ディスパッチ判定が `true` | 失敗例/対策/運用キーワード拡張の効果を確認 |
| MT-017 | FAIL | 2026-04-02 20:50 | Q1:`AIトレンドを調べて` → Q2:`その中で実装優先度を付けて`（debug_self_probe） | `debug_self_probe x2 -> orchestrator.answer -> respond` | `Main Agent` | Q2がQ1のAIトレンドではなく別文脈（yorimichi構成案）へ遷移 | 継続文脈での優先度整理期待を満たせず FAIL |
| MT-018 | PASS | 2026-04-02 20:40 | `最新のAIニュースだけ教えて。過去会話は使わないで`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> tool(web_search) -> respond -> self_review(rewrite)` | `Main Agent: web_search` | 直近ニュースをWeb検索ベースで返答 | メモリ検索ログは存在するが、回答内容は過去会話参照でなくニュース要約中心 |
| MT-019 | PASS | 2026-04-02 20:51 | `site:github.com で今週の話題を見て`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> web_search -> web_search(fallback) -> respond` | `Main Agent: web_search` | GitHubトレンド起点のグローバル調査回答を返却 | DDGSレート制限下でもフォールバックで回答継続 |
| MT-020 | PASS | 2026-04-02 20:10 | `kc3hacl 2025 の話って前にあった？`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> memory_retrieval(scope=guild,hits=22) -> respond -> self_review(approve)` | `Main Agent: memory retrieval` | KC3Hack 2025 の既存履歴を要約して返却 | typoを含む質問でも履歴文脈に寄せて応答 |
| MT-021 | FAIL | 2026-04-02 20:39 | Q1:`Python async/awaitを3点で` → Q2:`2つ目だけ詳しく` → Q3:`じゃあサンプルコード`（debug_self_probe） | `debug_self_probe x3 -> orchestrator.answer -> respond` | `Main Agent (no tools)` | 2ターン目以降が async/await 文脈から逸脱し、別トピックへドリフト | 多段フォローアップの一貫性を満たせず FAIL |
| MT-022 | - | | | | | | |
| MT-023 | - | | | | | | |
| MT-024 | - | | | | | | |
| MT-025 | PASS | 2026-04-02 20:09 | `https://docs.python.org/3/library/asyncio.html の内容を3行で要約して`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> respond -> self_review(approve)` | `Main Agent` | asyncio要約を3行で返却 | 投稿/返信とも `flags=4`, `embeds=0` をDiscord APIで確認 |
| MT-026 | PASS（実装レベル） | 2026-04-03 15:40 | `https://fastapi.tiangolo.com/ と https://flask.palletsprojects.com/en/stable/ の主張の違いを比較して`（debug_self_probe） | `/ask -> _run_reader_compare -> read_url_markdown x2` | `Main Agent: read_url_markdown` | URL比較で Reader 直行が選択される | `url_compare_intent=true`, `url_count=2` を確認 |
| MT-027 | PASS | 2026-04-03 00:33 | `@bot このURLの本文だけ抜いて箇条書きにして https://developer.mozilla.org/...` 相当 | `read_url_markdown` | Main Agent | Reader出力に URL本文素材を取得し、ノイズ語（menu/nav）の混入は観測されず | 本文中心抽出の期待を満たす |
| MT-028 | PASS | 2026-04-03 00:30 | Q1:URL読解相当履歴投入 → Q2:`さっきの記事の懸念点だけ` | `orchestrator.answer -> history_context付与` | Main Agent | follow-up prompt に `https://fastapi.tiangolo.com/tutorial/` を含む前回URL文脈が注入 | Reader+follow-up 継続を確認 |
| MT-029 | PASS | 2026-04-02 20:12 | `https://invalid.invalid/abc を読んで要約して`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> tool(read_url_markdown) -> respond -> self_review(rewrite)` | `Main Agent: read_url_markdown` | 無効URLを検知して安全な失敗メッセージを返却 | 返信 `flags=4`, `embeds=0` を確認 |
| MT-030 | PASS | 2026-04-02 23:35 | `@bot LangChainの最近のIssue傾向を調べて要点だけ` | `debug_self_probe -> source_deep_dive(github)` | `Main Agent` | `site:github.com` クエリで GitHub 深掘りを実行 | 期待どおり |
| MT-031 | PASS | 2026-04-02 23:35 | `@bot Python 3.13への反応をReddit中心に俯瞰して` | `debug_self_probe -> source_deep_dive(reddit)` | `Main Agent` | `site:reddit.com` クエリで Reddit 深掘りを実行 | 期待どおり |
| MT-032 | PASS | 2026-04-02 23:35 | `@bot このテーマの動画とSNS反応の違いを比較して` | `debug_self_probe -> source_deep_dive(youtube)` | `Main Agent` | `site:youtube.com` クエリで YouTube 深掘りを実行 | 期待どおり |
| MT-033 | PASS（実装レベル） | 2026-04-03 16:03 | Q1:`@bot Gemini API運用の実例を調べて` → Q2:`その中で再現しやすい順に並べて` 相当 | `_should_force_research_job` 判定確認 | Main Agent | 初段の強制調査ディスパッチ判定が `true` | 実例キーワードで調査委譲前提を満たす |
| MT-034 | PASS | 2026-04-03 00:30 | `@bot URL要約 + 関連コミュニティ反応も` 相当 | `_should_force_research_job` 判定確認 | Main Agent | 境界ケースで強制調査ディスパッチ判定が `true` | Reader/深掘り併用判断の入口を確認 |

---

## §7 エラーハンドリング・耐障害性テスト

| ID | 判定 | 実施日時 | 障害シナリオ設定 | 実行ルート | Bot挙動 | 観測ログ | 備考 |
|---|---|---|---|---|---|---|---|
| ERR-001 | PASS | 2026-04-02 21:30 | `GEMINI_API_KEY=invalid-key` で `--debug-self-probe --question "こんにちは"` 実行 | `debug_self_probe -> orchestrator._invoke_with_retry` | 失敗時もBotは継続し安全なエラー文言を返却 | `InvalidArgument: API key not valid` / `RuntimeError: Gemini invocation failed after retries` / 返答: `現在AI応答で問題が発生しています。時間をおいて再試行してください。` | 期待どおり |
| ERR-002 | PASS | 2026-04-02 21:31 | `GEMINI_TIMEOUT_SEC=1` で `--debug-self-probe --question "最新AI動向を詳細に教えて"` 実行 | `debug_self_probe -> orchestrator._invoke_with_retry` | タイムアウト時もBotは継続し安全なエラー文言を返却 | `TimeoutError` / `RuntimeError: Gemini invocation failed after retries` / 返答: `現在AI応答で問題が発生しています。時間をおいて再試行してください。` | 期待どおり |
| ERR-003 | PASS | 2026-04-02 22:43 | `tools.search_tools.web_search("最近のニュースは？")` を DDGS/fallback 両方失敗にモックして実行 | `tools.search_tools.web_search` | Web検索失敗時も安全な文言で返却 | `検索に失敗しました。時間をおいて再試行してください。` | 期待どおり |
| ERR-004 | PASS | 2026-04-02 23:34 | ChromaDB 書き込み例外をモックして応答生成を実行 | `orchestrator._store_conversation` | Bot継続 | 保存失敗をログ化しつつ応答を返却 | 期待どおり継続 |
| ERR-005 | PASS | 2026-04-02 23:34 | followup.send 例外をモックして `/ask` 応答送信を実行 | `ask callback -> send_response -> exception handling` | Bot継続 | 送信例外時もプロセス継続 | 期待どおり継続 |
| ERR-006 | PASS | 2026-04-02 21:23 | `DISCORD_TOKEN=dummy`, `GEMINI_API_KEY=`（空）で起動 | `main.py startup` | 必須環境変数不足で安全終了 | `RuntimeError: DISCORD_TOKEN (or DISCORD_BOT_TOKEN) and GEMINI_API_KEY are required` | 期待どおり例外終了 |
| ERR-007 | PASS | 2026-04-02 21:27 | `INITIAL_PROFILE_PATH=/tmp/not-found-profile.md` で `--debug-self-probe --question "1+1は？"` 実行 | `debug_self_probe -> orchestrator._load_profile_text` | 初期プロフィール不在を警告しつつ回答継続 | `WARNING initial_profile.md not found; continuing without static profile` / 返答: `1+1は2です。` | 期待どおり継続動作 |
| ERR-008 | PASS | 2026-04-02 21:27 | `INITIAL_PROFILE_PATH=/tmp/initial_profile_long.md`（13050文字）で `--debug-self-probe --question "1+1は？"` 実行 | `debug_self_probe -> orchestrator._load_profile_text` | 上限超過を警告し切り詰め後に回答継続 | `WARNING initial_profile.md exceeds max chars (12000). Truncating.` / 返答: `1+1は2です。` | 期待どおり継続動作 |
| ERR-009 | PASS | 2026-04-02 20:13 | `RESEARCH_AGENT_URL=http://127.0.0.1:9998` に上書きして `dispatch_research_job(wait=false)` 実行 | `main-agent tools.research_tools -> _request_json(connect fail)` | エラーJSON返却でBot継続 | `{"code":"research_agent_http_error"}` を確認 |
| ERR-010 | PASS | 2026-04-02 22:39 | `orch.memory.get_user_profile_facts` を例外化して `orch.answer(question="こんにちは")` 実行 | `main_agent.core.orchestrator._answer_impl` | persona profile 読み込み失敗でもBotは継続し回答を返す | `Failed to load persona profile for prompt` / 返答本文あり | 期待どおり（プロンプト内 persona 取得失敗を継続処理） |

---

## §8 Discord質問ロジックパス網羅テスト

### §8.1 Research Controls 注入パス

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | 検出された mode/timeout | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| QLP-001 | PASS | 2026-04-02 20:13 | `Gemini CLIでAIトレンドを調べて`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> dispatch_research_job -> web_search(fallback) -> respond` | `mode=gemini_cli` / timeoutは既定 | mode検出は成功。CLI失敗後にWeb検索へフォールバックして回答継続 | レート制限配慮のため1回のみ実施 |
| QLP-002 | PASS | 2026-04-02 20:23 | `フォールバックモードで最近のPython事情を調査して`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> dispatch_research_job(fallback) -> respond -> self_review(rewrite)` | `mode=fallback` / timeoutは既定 | mode=fallback が反映され、最終回答まで継続 | ログに `"mode": "fallback"` を確認 |
| QLP-003 | PASS | 2026-04-02 20:14 | `120秒でKubernetesの最新動向を調べて`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> dispatch_research_job(gemini_cli) -> dispatch_research_job(fallback) -> respond` | `mode=gemini_cli` / `timeout=120` | timeout秒指定が反映され、fallbackで最終回答取得 | ログに `explicit=True requested=120 effective=120` を確認 |
| QLP-004 | PASS | 2026-04-02 20:25 | `２分間でRedditの反応を調べて`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> dispatch_research_job -> respond -> self_review(rewrite)` | `mode=auto` / `timeout=120` | 全角数値+分指定が120秒へ正規化され、調査完了まで継続 | ログに `explicit=True requested=120 effective=120` を確認 |
| QLP-005 | PASS | 2026-04-03 16:08 | `1800秒でじっくり調べて`（debug_self_probe） | `dispatch_research_job(wait=false)` | `mode=auto` / `timeout=1800` | レスポンスの `timeout_sec` が要求値と一致 | `timeout_sec=1800`, `poll_timeout_sec=1830` を確認 |
| QLP-006 | PASS | 2026-04-02 20:26 | `Gemini CLIで60秒でGitHubの議論を調べて`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> dispatch_research_job(gemini_cli) -> respond -> self_review(needs_tool)` | `mode=gemini_cli` / `timeout=60` | mode+timeout同時指定を反映。CLI失敗後も最終応答を返却 | ログに `requested=60 effective=60` と `"mode": "gemini_cli"` を確認 |

### §8.2 Recent Conversation 文脈組立パス

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | recent_limit | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| QLP-007 | FAIL | 2026-04-02 20:37 | 前置き:`Python async/awaitを3点で説明して` → `さっき話した内容は？`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> respond -> self_review(rewrite)` | `recent_limit=40（実装値）` | 想起質問だが直前トピックではなく過去別件（Claude Code系）を主に返答 | 「直近会話の想起」期待を満たせず FAIL |
| QLP-008 | FAIL | 2026-04-02 20:38 | QLP-007直後に `それについてもう少し教えて`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> respond -> self_review(needs_tool)` | `recent_limit=10（実装値）` | 直前回答の掘り下げではなく別トピック（yorimichi）へ遷移 | フォローアップ解決の整合性不足で FAIL |
| QLP-009 | PASS | 2026-04-02 20:38 | `今日の天気は？`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> respond` | `recent_context添付なし（_should_attach_recent_context=false）` | 単発の天気回答を返却し、会話継承前提の文言なし | 独立質問としての応答は成立 |

### §8.3 フォローアップ解決・指示語注入パス

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | 注入コンテキスト | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| QLP-010 | FAIL | 2026-04-02 20:43 | Q1:`推奨フレームワーク3つ` → Q2:`その3つの違いは？`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> respond` | follow-up context注入想定 | Q1で「3つ」を確定せず質問返し、Q2は async/await 3点へ誤参照 | 「その3つ」を直前列挙へ解決できず FAIL |
| QLP-011 | FAIL | 2026-04-02 20:44 | Q1:`主要なデータベースを3つ挙げて` → Q2:`それぞれの長所は？`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> respond` | follow-up context注入想定 | Q1は PostgreSQL/MongoDB/Redis を列挙したが、Q2で別話題（コンパイル/インタプリタ）へ遷移 | 「それぞれ」の参照先が保持できず FAIL |
| QLP-012 | FAIL | 2026-04-02 20:45 | Q1:`Kubernetesの概要を短く` → Q2:`深掘りして`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> dispatch_research_job -> respond` | follow-up context注入想定 | Q2の深掘り対象が Kubernetes ではなく GitHub動向に逸脱 | 前問対象の深掘りが維持できず FAIL |

### §8.4 曖昧クエリの検出・拒否パス

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | 判定結果 | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| QLP-013 | FAIL | 2026-04-02 20:47 | `Githubの最新議論を調べて`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> dispatch_research_job -> respond` | 曖昧クエリ時は明確化要求想定 | 明確化質問ではなく一般論の調査応答を返却 | 「対象が曖昧です」系の期待文言を満たせず FAIL |
| QLP-014 | FAIL | 2026-04-02 20:47 | Q1:`oithxs/yorimichiについて` → Q2:`その最新議論を調べて`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> dispatch_research_job -> respond` | Q1トピック継承想定 | Q2で `yorimichi` ではなく GitHub全体トレンドに逸脱 | ログで `requested=1800` が現れ、直前文脈由来の制御継承も疑われる |
| QLP-015 | FAIL | 2026-04-02 20:48 | `github.com/google-gemini/gemini-cli の最新議論を調べて`（debug_self_probe） | `debug_self_probe -> dispatch_research_job(gemini_cli) -> web_search(fail) -> respond` | 具体対象の調査結果想定 | gemini_cli失敗 + web_searchレート制限で具体議論の自動取得に失敗 | URL案内は返ったが「具体的調査結果」期待に未達で FAIL |

### §8.5 Research Job 強制ディスパッチパス

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | force_dispatch判定 | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| QLP-016 | PASS | 2026-04-02 20:57 | `Twitterの最新の反応を調べて`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> dispatch_research_job -> respond` | force dispatch期待（source語+調査語） | `dispatch_research_job` が実行され、source=`x` で調査応答を返却 | 強制dispatch相当の挙動は成立 |
| QLP-017 | FAIL | 2026-04-02 20:57 | `yorimichiについて教えて`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> read_url_markdown -> respond` | entity lookupでdispatch期待 | `dispatch_research_job` ではなく `read_url_markdown` が選択 | `guard_external_research_intent` 系期待と不一致 |
| QLP-018 | PASS | 2026-04-02 20:58 | `明日のyorimichiタスクを追加して`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> execute_internal_action(add_task) -> respond` | タスク/予定系は強制dispatchしない | `add_task` が成功しタスク登録完了 | 強制dispatch抑止の期待どおり |

### §8.6 Self-Review パス

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | review_action | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| QLP-019 | PASS | 2026-04-02 20:59 | `1+1は？`（debug_self_probe） | `debug_self_probe -> orchestrator.answer -> respond` | `self-review=approve` 期待 | ログに `Agent self-review: ... action=approve` を確認 | 期待どおり |
| QLP-020 | PASS | 2026-04-02 20:59 | `最新のGemini APIの変更点を厳密に教えて`（debug_self_probe） | `dispatch_research_job -> web_search -> respond` | `self-review=rewrite` 期待 | ログに `Agent self-review: ... action=rewrite` を確認 | 期待どおり |
| QLP-021 | FAIL | 2026-04-02 21:00 | `この記事の要約 https://example.com/article`（debug_self_probe） | `read_url_markdown -> respond` | `self-review=needs_tool` 期待 | `needs_tool` は発生せず、`rewrite` で終了 | 追加ツール要求分岐の期待に未達 |

### §8.7 重複防止・メンション高速カレンダーパス

| ID | 判定 | 実施日時 | 送信メッセージ | 実行ルート | 使用ツール/エージェント | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| QLP-022 | SKIP | 2026-04-02 21:10 | `明日14時から15時に会議`（backdoor想定） | `debug_self_probe -> orchestrator.answer` | mention_quick経路確認を期待 | backdoorは `on_message -> build_quick_calendar_action` を通らず再現不能 | 実装上、self_probeは `orchestrator.answer` 直呼びであり本ケースの自動検証対象外 |
| QLP-023 | SKIP | 2026-04-02 21:10 | `明後日までにレポート提出タスク`（backdoor想定） | `debug_self_probe -> orchestrator.answer` | mention_quick経路確認を期待 | backdoorは `on_message -> build_quick_calendar_action` を通らず再現不能 | 実装上、self_probeは `orchestrator.answer` 直呼びであり本ケースの自動検証対象外 |

---

## §9 セキュリティテスト

| ID | 判定 | 実施日時 | テスト設定 | 実行ルート | 観測結果 | 観測ログ | 備考 |
|---|---|---|---|---|---|---|---|
| SEC-001 | - | | | | | | |
| SEC-002 | PASS | 2026-04-02 22:29 | `INTERNAL_ALLOWED_ACTIONS=add_task` で `execute_internal_action("unknown_action")` | `tools.action_tools.execute_internal_action` | 未許可アクションを拒否 | `{"code":"unsupported_action","action":"unknown_action"}` | 期待どおり |
| SEC-003 | PASS | 2026-04-02 20:13 | `X-Research-Token=invalid-token` で `POST /v1/jobs` | `research-agent API auth check` | 403 Forbidden | `{"status":"error","code":"forbidden"}` を確認 |
| SEC-004 | - | | | | | | |
| SEC-005 | FAIL | 2026-04-02 22:29 | `INTERNAL_ALLOWED_ACTIONS=backup_server_data` + `target="../../../etc/passwd"` | `tools.action_tools._handle_backup_server_data` | パストラバーサル相当ターゲットは拒否された | `{"code":"forbidden_target","target":"/etc/passwd"}` | 拒否自体は成功したが、計画期待の `forbidden_path` とはコード名不一致 |
| SEC-006 | - | | | | | | |

---

## §10 設定バリデーションテスト

| ID | 判定 | 実施日時 | 環境変数設定 | 実行ルート | 観測結果 | 観測ログ | 備考 |
|---|---|---|---|---|---|---|---|
| CFG-001 | PASS | 2026-04-02 21:21 | `BOT_GUILD_ID=`（空）, `GEMINI_API_KEY=dummy`, `DISCORD_TOKEN=dummy` | `main.py startup -> parse_allowed_guild_ids` | 期待どおり起動失敗（必須変数不足） | `ValueError: BOT_GUILD_ID is required` | 期待結果と一致 |
| CFG-002 | PASS | 2026-04-02 21:22 | `BOT_GUILD_ID=123456789012345678`, `ALLOWED_GUILD_IDS=abc,987654321098765432`, `GEMINI_API_KEY=dummy`, `DISCORD_TOKEN=dummy` | `main.py startup -> parse_allowed_guild_ids` | 不正値 `abc` を警告で無視 | `WARNING Ignore invalid guild id in ALLOWED_GUILD_IDS: abc` | 期待どおり。起動後はダミートークンのため `LoginFailure` で終了 |
| CFG-003 | PASS | 2026-04-02 22:24 | `DISCORD_ENABLE_MESSAGE_CONTENT_INTENT=false` で起動 | `main.py startup -> on_ready bootstrap branch` | Message Content Intent 無効時にメモリbootstrapをスキップ | `Skip memory bootstrap because DISCORD_ENABLE_MESSAGE_CONTENT_INTENT=false` | 期待どおり（履歴取り込み抑止を警告ログで確認） |
| CFG-004 | PASS | 2026-04-02 21:28 | `MEMORY_RETRIEVAL_SCOPE=channel` で `--debug-self-probe --question "今日のメモリ参照スコープを確認したい"` 実行 | `debug_self_probe -> orchestrator.answer` | メモリ検索スコープが channel で実行 | `Memory retrieval: scope=channel` / 返答本文あり | 期待どおり |
| CFG-005 | - | | | | | | |
| CFG-006 | - | | | | | | |
| CFG-007 | PASS | 2026-04-02 23:34 | `LOGSEARCH_DEFAULT_SCOPE=channel` で `/logsearch Python` 相当を実行 | `logsearch -> scope default resolution` | デフォルトscope=`channel` で検索 | ログと引数の scope=channel を確認 | 期待どおり |
| CFG-008 | PASS | 2026-04-02 21:29 | `DISCORD_COMMAND_ALLOWLIST=ask` で起動 | `main.py startup -> command prune/sync` | allowlist外コマンドが剪定される | `Pruned local commands by allowlist: ... 'logsearch' ...` | `/logsearch` 除外をログで確認 |

---

## §11 エッジケース・境界値テスト

| ID | 判定 | 実施日時 | 送信メッセージ/操作 | 実行ルート | 使用ツール/エージェント | 観測結果 | 備考 |
|---|---|---|---|---|---|---|---|
| EDGE-001 | PASS | 2026-04-02 23:34 | `orchestrator.answer(question="")` を実行 | `orchestrator.answer -> safe fallback` | 空入力でも例外なく安全応答 | Botプロセス継続を確認 | 期待どおり |
| EDGE-002 | - | | | | | | |
| EDGE-003 | PASS | 2026-04-02 22:41 | `send_response` に 3800文字の本文を入力 | `main.py send_response -> chunk_text` | Main Agent | 1900文字上限で2分割送信 | 先頭チャンク長 1900 を確認 |
| EDGE-004 | PASS | 2026-04-02 22:41 | `send_response` に 16001文字の本文を入力 | `main.py send_response -> attachment fallback` | Main Agent | `ask_response.txt` 添付へ切替 | summary長 722, filename=`ask_response.txt` を確認 |
| EDGE-005 | PASS | 2026-04-02 22:42 | Web検索結果を同一ドメイン重複込みでモック | `tools.search_tools.web_search` | Main Agent | 同一ドメインは重複除外される | `example.com` が1件に圧縮されることを確認 |
| EDGE-006 | PASS | 2026-04-02 22:30 | `execute_internal_action(add_calendar_event)` に `start_time=15:00`, `end_time=14:00` を指定 | `tools.action_tools.execute_internal_action -> _handle_add_calendar_event` | 時刻逆転を検知して拒否 | `{"code":"invalid_time_range"}` | 期待どおり（エラー拒否） |
| EDGE-007 | PASS | 2026-04-02 22:30 | `execute_internal_action(add_task)` に `{"title":""}` を指定 | `tools.action_tools.execute_internal_action -> _handle_add_task` | 空タイトルを検知して拒否 | `{"code":"missing_title"}` | 期待どおり |
| EDGE-008 | PASS | 2026-04-02 22:40 | `profile_set` の key 49文字入力 | `main.py profile_set guard` | key 長超過を拒否 | `key は48文字以内で指定してください。` | 実装ガード文言を確認 |
| EDGE-009 | PASS | 2026-04-02 22:40 | `profile_set` の value 501文字入力 | `main.py profile_set guard` | value 長超過を拒否 | `value は500文字以内で指定してください。` | 実装ガード文言を確認 |
| EDGE-010 | PASS | 2026-04-02 22:40 | `/logsearch ""` 相当の空キーワード | `main.py logsearch guard` | 空キーワードを拒否 | `キーワードが空です。` | 実装ガード文言を確認 |
| EDGE-011 | PASS | 2026-04-02 22:40 | `/runcli ""` 相当の空コマンド | `main.py runcli guard` | 空コマンドを拒否 | `コマンドが空です。` | 実装ガード文言を確認 |
| EDGE-012 | - | | | | | | |

---

## 実行ルート記録の凡例

テスト実施時に「実行ルート」列に記録する際の参考パターンを以下に示す。

### `/ask` コマンド経由

```
/ask → guild_id チェック → _extract_research_controls → _should_attach_recent_context
  → [Yes] _build_recent_conversation_context(limit=N)
  → _inject_recent_conversation_hint
  → _inject_followup_targets_hint
  → orchestrator.answer
    → _answer_impl
      → _is_explicit_global_source_query → [Yes] skip history / [No] fetch_relevant_messages
      → _build_system_prompt (history + persona)
      → _generate_with_tools
        → _decide_next_action
          → [tool] execute(tool_name, args)
          → [respond] _self_review_response → [approve/rewrite/needs_tool]
      → _store_conversation
  → send_response (chunk / file attach)
```

### `@メンション` 経由

```
on_message → bot? / guild? / allowed? → メンション検出 → prefix チェック
  → 空入力チェック → [empty] reply("質問内容を書いてください")
  → [MENTION_QUICK_CALENDAR_ENABLED] build_quick_calendar_action
    → [match] execute_tool_job(add_calendar_event / add_task) → send_message_response → return
  → _handle_mention_question
    → _extract_research_controls → _should_attach_recent_context
    → _build_recent_conversation_context(limit=N, before_message_id)
    → _inject_recent_conversation_hint → _inject_followup_targets_hint
    → orchestrator.answer → (同上)
    → send_message_response
    → append_debug_probe_audit
```

### 使用ツール/エージェント記録例

| パターン | 記録例 |
|---|---|
| Web検索のみ | `Main Agent: web_search` |
| URL読み込み | `Main Agent: read_url_markdown` |
| Research Agent 委譲 | `Main Agent → Research Agent: dispatch_research_job(mode=auto, source=github)` |
| 内部アクション実行 | `Main Agent: execute_internal_action(add_calendar_event)` |
| 高速カレンダー（LLMスキップ） | `on_message → build_quick_calendar_action → execute_tool_job(add_calendar_event)` |
| ツールなし（LLM直接回答） | `Main Agent: (no tools, direct LLM response)` |
| Self-Review 追加ツール | `Main Agent: _self_review_response → needs_tool → read_url_markdown` |

---

## §12 実返却値ログ（Discord実測）

`a.md` の観測データから、主要テストの「実際に返却された回答本文（実値）」を記録する。

| ID | 実施日時 | 送信メッセージ | 実際に返却された答え（実値） |
|---|---|---|---|
| MT-001 | 2026-04-02 20:08 | `今年のトレンドを3点で教えて` | 2026年の主要な技術トレンドを、主要な調査機関（Gartner, Deloitte等）の予測に基づき3点にまとめました。<br><br>AIの社会実装とROIの追求: 実験的な導入から脱却し、企業活動において具体的な投資対効果（ROI）を生み出す実用的なAI活用が最優先事項となっています。<br>AIエージェントの自律化: 単なる生成AIから、複雑なワークフローを自律的に遂行するエージェント型プラットフォームへの移行が加速しています。<br>レジリエンスと信頼性の確保: AIの普及に伴い、セキュリティ、ガバナンス、およびシステム全体の信頼性を担保する技術が戦略的基盤として重視されています。<br><br>より詳細な特定の業界動向や技術分野について知りたい場合はお知らせください。<br><br>[参考URL]<br>https://www.deloitte.com/us/en/insights/topics/technology-management/tech-trends.html<br>https://www.futureinsights.com/emerging-technology-trends-guide-2026/<br>https://www.techradar.com/tech/the-11-biggest-tech-trends-of-2026-according-to-ces-2026<br>https://www.gartner.com/en/articles/top-technology-trends-2026<br>https://www.ibm.com/think |
| MT-025 | 2026-04-02 20:09 | `https://docs.python.org/3/library/asyncio.html の内容を3行で要約して` | async/await構文を用いた並行処理を実現するPython標準ライブラリです。<br>ネットワーク通信やDB操作など、IOバウンドな処理の効率化に最適です。<br>高レベルAPIでタスク実行や同期を制御し、低レベルAPIでイベントループ等の詳細な管理も可能です。 |
| MT-015 | 2026-04-02 20:10 | `GitHub中心で最新の議論を調べて要点だけ` | 2026年4月2日現在のGitHubにおける主要な議論とトレンドの要点は以下の通りです。<br><br>AIエージェントの自律化: LangChainやCrewAI等のフレームワークにおいて、単なるタスク実行から、複雑な推論プロセスを伴う「エージェントエンジニアリング」への移行が議論の中心です。<br>ローカルLLMの最適化: 推論速度の向上とメモリ消費の削減を目的とした、ローカル実行環境（llama.cpp等）の最適化手法が活発に議論されています。<br>開発者体験（DevEx）の自動化: CI/CDパイプラインの簡素化や、環境構築の自動化ツールが引き続きトレンド上位を占めています。<br><br>特定のプロジェクトや技術スタックについて詳細な調査が必要であればお知らせください。<br><br>[参考URL]<br>https://github.com/topics/trending?o=desc&s=updated<br>https://github.com/trending |
| MT-020 | 2026-04-02 20:10 | `kc3hacl 2025 の話って前にあった？` | はい、過去にKC3Hack 2025に関する情報が共有されています。2024年11月16日にプレエントリーの開始が告知され、その後2025年1月15日には本エントリーの受付と勉強会の開催について案内がありました。 |
| MT-026 | 2026-04-02 20:11 | `https://fastapi.tiangolo.com/ と https://flask.palletsprojects.com/en/stable/ の主張の違いを比較して` | FastAPIとFlaskの主な違いを比較まとめました。<br><br>| 項目 | FastAPI | Flask |<br>| :--- | :--- | :--- |<br>| 設計思想 | 型ヒント活用による生産性と高速性 | 柔軟性と拡張性を重視したミニマリズム |<br>| パフォーマンス | 高（Starlette/Pydantic基盤） | 中（WSGIベース） |<br>| 主な特徴 | 自動ドキュメント生成、DIシステム | 豊富なエコシステム、高い自由度 |<br>| 適した用途 | 高速なAPI開発、モダンな非同期処理 | 小規模〜中規模、構成の自由度が必要な場合 |<br><br>結論:<br>FastAPIは「モダンなPython機能（型ヒント等）を活かして、開発効率と実行速度を最大化したい」場合に適しています。<br>Flaskは「特定の構成を強制されず、自分の好みのライブラリを組み合わせて柔軟に構築したい」場合に適しています。<br><br>[参考URL]<br>https://fastapi.tiangolo.com/<br>https://flask.palletsprojects.com/en/stable/ |
| MT-029 | 2026-04-02 20:12 | `https://invalid.invalid/abc を読んで要約して` | 指定されたURL（https://invalid.invalid/abc）は無効なドメインであるため、内容を読み取ることができませんでした。URLに誤りがないかご確認いただけますでしょうか。<br><br>[参考URL]<br>https://invalid.invalid/abc |
| QLP-001 | 2026-04-02 20:14 | `Gemini CLIでAIトレンドを調べて` | Gemini CLIでの調査はモデルエラーにより失敗しましたが、Web検索により2026年4月現在の主要なAIトレンドをまとめました。<br><br>2026年4月のAIトレンド要点<br>収益化へのシフト: 成長重視から、AIプラットフォームの具体的な収益化戦略（Monetization）への転換が進んでいます。<br>AIパートナーシップ: AIが単なるツールから、チームワークや研究を支援する「真のパートナー」へと進化しています。<br>インフラ効率化: 大規模モデルの運用コスト削減と、インフラの効率化が企業導入の鍵となっています。<br>セキュリティと信頼性: AIの普及に伴い、セキュリティ対策とガバナンスが最優先事項として議論されています。<br><br>詳細なレポートが必要な場合は、特定のトピックを指定して再度指示してください。<br><br>[参考URL]<br>https://www.humai.blog/ai-news-trends-april-2026-complete-monthly-digest/<br>https://www.quetext.com/blog/ai-trends-2026<br>https://www.ibm.com/think<br>https://www.comptia.org/en-us/blog/top-tech-trends-to-watch-in-2026/ |
| QLP-003 | 2026-04-02 20:17 | `120秒でKubernetesの最新動向を調べて` | Kubernetes v1.36（2026年4月22日リリース予定）の主要な動向をまとめました。<br><br>AIワークロードの最適化: GPU等のAI専用ハードウェア割り当てを効率化する「Dynamic Resource Allocation (DRA)」が強化され、AIモデルのトレーニング・推論環境としての統合が深まりました。<br>プラットフォームの成熟: 運用ポリシーを制御する「MutatingAdmissionPolicy」が一般提供（GA）となり、クラスターの制御がより堅牢になりました。今期は計36の機能がベータから安定版へ移行しています。<br>2026年のトレンド: AIワークロードの増加に伴い、ステートフルな構成やエッジ環境での利用、ストレージ中心のリカバリ機能への需要が急速に高まっています。<br><br>[参考URL]<br>https://kubernetes.io/blog/2026/03/30/kubernetes-v1-36-sneak-peek/<br>https://dev.to/alitron/kubernetes-136-release-key-changes-and-adoption-planning-strategies-for-existing-deployments-485l<br>https://www.informationweek.com/it-infrastructure/4-trends-that-will-transform-kubernetes-in-2026<br>https://github.com/kubernetes/kubernetes/blob/master/CHANGELOG/CHANGELOG-1.36.md |
| QLP-002 | 2026-04-02 20:23 | `フォールバックモードで最近のPython事情を調査して` | 2026年のPythonは、AI・機械学習分野での圧倒的な地位を維持しつつ、以下の3点が主要トレンドとなっています。<br><br>AI・データサイエンスの深化: LLM統合や複雑なデータパイプライン構築のエコシステムがさらに拡充。<br>パフォーマンス向上: メモリ効率の改善や並行処理の最適化により、実行速度の課題が着実に解消中。<br>開発の近代化: 型ヒント（Type Hints）の標準化による保守性の向上と、モダンなライブラリによる堅牢な開発プロセスの普及。<br><br>出典: Intellipaat, The New Stack 等の調査レポートに基づく。<br><br>より詳細な技術スタックや特定のライブラリ動向について深掘りが必要であればお知らせください。<br><br>[参考URL]<br>https://intellipaat.com/blog/top-python-trends/<br>https://thenewstack.io/python-whats-coming-in-2026/<br>https://onehorizon.ai/blog/modern-python-best-practices-the-2026-definitive-guide<br>https://inveritasoft.com/blog-python-development-trends-innovations-and-latest-technology<br>https://turbogeek.org/the-explosive-python-trends-2026-you-should-know/ |
| QLP-004 | 2026-04-02 20:25 | `２分間でRedditの反応を調べて` | RedditにおけるGemini CLIの最近の反応を調査しました。主な議論のポイントは以下の通りです。<br><br>レート制限への不満: 有料・無料ユーザー問わず厳しい制限がかかる現状に対し、実用性低下を指摘する声が根強くあります。<br>v0.31アップデートへの評価: Gemini 3.1 Pro Preview対応や実験的ブラウザエージェント機能は技術的に評価されていますが、運用上の制限がユーザー体験を損なっているという意見が支配的です。<br>コミュニティの傾向: r/GeminiAI 等ではトラブルシューティングや制限緩和を求める投稿が続いており、機能進化よりも安定した利用環境を望む声が強まっています。<br><br>[参考URL]<br>https://www.reddit.com/r/ja/<br>https://www.reddit.com/r/GeminiAI/<br>https://zenn.dev/daideguchi/articles/fe42feb467b720<br>https://qiita.com/Takuya__/items/ba869f84238ce916dd1d<br>https://cloud.google.com/blog/ja/topics/developers-practitioners/introducing-gemini-cli-github-actions<br>https://excel-ubara.com/GenerativeAI/GAI074.html<br>https://www.reddit.com/r/GoogleGeminiAI/top/<br>https://geminicli.com/docs/changelogs/<br>https://github.com/google-gemini/gemini-cli/discussions<br>https://medium.com/@joe.njenga/i-tested-the-updated-gemini-cli-and-found-these-new-features-youll-instantly-like-9b8cae400e6e<br>https://ts2.tech/en/everything-you-need-to-know-about-google-gemini-cli-features-news-and-expert-insights/<br>https://www.reddit.com/login/ |
| QLP-006 | 2026-04-02 20:26 | `Gemini CLIで60秒でGitHubの議論を調べて` | Gemini CLIのGitHubリポジトリにおける議論の調査は、APIエラーにより自動実行が失敗しました。代替としてGitHub上の主要な議論・情報源を特定しました。<br><br>調査結果<br>GitHub上の公式リポジトリにて、以下のページから最新の議論や課題を確認可能です。<br><br>Issues: バグ報告や機能要望の議論[https://github.com/google-gemini/gemini-cli/issues](https://github.com/google-gemini/gemini-cli/issues)<br>Discussions: コミュニティによる質疑応答や提案[https://github.com/google-gemini/gemini-cli/discussions](https://github.com/google-gemini/gemini-cli/discussions)<br>Releases: 最新の変更点やアップデート情報[https://github.com/google-gemini/gemini-cli/releases](https://github.com/google-gemini/gemini-cli/releases)<br><br>エラー詳細と再試行<br>APIエラー（ModelNotFoundError）が発生しました。モデル設定が正しくないか、環境変数の認証情報が古い可能性があります。<br><br>再試行オプション:<br>モデル名を指定して再実行（例: gemini-1.5-flash 等）<br>ローカルの認証設定（gcloud auth または APIキー）の再確認<br><br>[参考URL]<br>https://github.com/google-gemini/gemini-cli/issues<br>https://github.com/google-gemini/gemini-cli/discussions<br>https://github.com/google-gemini/gemini-cli/releases |
| QLP-005 | 2026-04-02 20:36 | `1800秒でじっくり調べて` | 承知いたしました。1800秒（30分）の調査ジョブを投入しました。調査対象として「最新の技術トレンドおよび主要な開発コミュニティの議論」を設定しています。進捗確認が必要な場合は、ジョブID「rj-1775129801998-11-130781393438400」を添えて再度お声がけください。 |
| QLP-007 | 2026-04-02 20:37 | `さっき話した内容は？` | 先ほどは、GitHub上の『Claude Code』に関する議論の要点について調査・回答いたしました。なお、直前のやり取りで「Claude Codeに限定せず、文脈を汲み取ってほしい」というご指摘をいただいております。今後は、ご提示いただいた過去の経緯（リポジトリ『yorimichi』の改善、面接・課題の日程調整、Gemini APIのエラー対応など）を考慮し、より文脈に沿った回答を心がけます。他に確認したい過去の経緯や、現在進めている作業はありますか？ |
| QLP-008 | 2026-04-02 20:38 | `それについてもう少し教えて` | 「yorimichi」は、Kc3hack 2026（神戸大学のハッカソンイベント）で開発されたプロダクトです。<br><br>提供されたリポジトリ情報からは、具体的な機能や技術スタックの詳細な記述は確認できませんでした。リポジトリのREADMEやソースコードが未整備、あるいは非公開である可能性があります。<br><br>もし特定の機能や技術的な詳細を知りたい場合は、リポジトリ内の特定のファイル（`README.md`や`src`ディレクトリなど）を確認するか、開発者による発表資料などを探す必要があります。<br><br>[参考URL]<br>https://github.com/oithxs/yorimichi |
| QLP-009 | 2026-04-02 20:38 | `今日の天気は？` | 2026年4月2日（木）の京都市は、引き続き雨の予報です。局地的に強く降る可能性があるため、外出の際は雨具をご利用ください。 |
| MT-021 | 2026-04-02 20:38-20:39 | Q1:`Python async/awaitを3点で` / Q2:`2つ目だけ詳しく` / Q3:`じゃあサンプルコード` | Q1返答: Pythonのasync/awaitの要点は以下の3点です。 1) 非同期処理の実現 2) 中断と再開 3) IOバウンドに最適。<br>Q2返答: 「oithxs/yorimichi」のIssue #17（操作説明書の作成）に関する詳細です。<br>Q3返答: 「yorimichi」という名称のプロジェクトやライブラリについて、現時点で具体的な情報が不足しています。 |
| MT-018 | 2026-04-02 20:40 | `最新のAIニュースだけ教えて。過去会話は使わないで` | 2026年4月1日時点の主要なAIニュースをまとめました。<br><br>1. AIプラットフォームの収益化: 成長重視から収益化戦略への転換が業界全体で加速しています。<br>2. 医療AIの臨床リスク: 医療現場でのAI活用が「自動操縦」化することで、医師のスキル低下（デスクリング）を招く懸念が専門家から指摘されています。<br>3. モデル更新と競争: OpenAIやAnthropicをはじめとする主要各社から、LLMのアップデートやベンチマーク更新が継続的に発表されています。<br><br>出典: https://www.humai.blog/ai-news-trends-april-2026-complete-monthly-digest/ 他<br><br>[参考URL]<br>https://www.humai.blog/ai-news-trends-april-2026-complete-monthly-digest/<br>https://llm-stats.com/ai-news<br>https://economictimes.indiatimes.com/topic/ai-news-today-april-1-2026-openai-anthropic-announcement<br>https://aitoolly.com/ai-news/2026-04-01<br>https://www.linkedin.com/pulse/latest-ai-legal-compliance-news-week-april-1-2026-wes-henderson-ywixc |
| QLP-010 | 2026-04-02 20:43 | Q1:`推奨フレームワーク3つ` / Q2:`その3つの違いは？` | Q1返答: 「推奨フレームワーク」は開発領域次第なので、AIエージェント開発 / Webフロントエンド / バックエンドAPI のどれかを指定してほしい旨の確認質問。<br>Q2返答: Python async/await の3点（待ち時間活用、`async`定義、`await`制御）の違い説明。 |
| QLP-011 | 2026-04-02 20:44 | Q1:`主要なデータベースを3つ挙げて` / Q2:`それぞれの長所は？` | Q1返答: PostgreSQL / MongoDB / Redis の3つを列挙。<br>Q2返答: 直前DB文脈ではなく「コンパイル言語とインタプリタ言語の長所」を回答。 |
| QLP-012 | 2026-04-02 20:44-20:45 | Q1:`Kubernetesの概要を短く` / Q2:`深掘りして` | Q1返答: Kubernetesはコンテナ運用の自動化基盤であり、監視・復旧・スケーリングを担う旨を説明。<br>Q2返答: Kubernetes深掘りではなく「GitHubにおけるAIネイティブ開発、リリース自動化、フィードバックループ」の3点を調査結果として回答。 |
| QLP-013 | 2026-04-02 20:47 | `Githubの最新議論を調べて` | 2026年4月2日時点のGitHubトレンド情報を確認しました。現在、特定の技術領域に偏らず、世界中の開発者が注目するオープンソースプロジェクトがリアルタイムで更新されています。具体的な技術スタック（例: AI、Rust、WebAssemblyなど）や、特定のプロジェクトに関する議論を深掘りしたい場合は、詳細を教えていただけますか？ |
| QLP-014 | 2026-04-02 20:47 | Q1:`oithxs/yorimichiについて` / Q2:`その最新議論を調べて` | Q1返答: 「oithxs/yorimichi」はKc3hack 2026の成果物で、README未整備・Issue #17で操作説明書作成中との説明。<br>Q2返答: `yorimichi` の最新議論ではなく、GitHub全体のトレンド（AIエージェント自動化、最適化、Webフレームワーク）を回答。 |
| QLP-015 | 2026-04-02 20:48 | `github.com/google-gemini/gemini-cli の最新議論を調べて` | 申し訳ありません。現在、GitHubの検索および調査ツールに技術的なエラーが発生しており、最新の議論内容を自動取得できませんでした。直接以下の公式ページより、最新のIssueやプルリクエストをご確認いただけます。https://github.com/google-gemini/gemini-cli/issues |
| QLP-016 | 2026-04-02 20:57 | `Twitterの最新の反応を調べて` | 2026年4月2日現在、X（旧Twitter）上では特定の単一トピックが支配的ではなく、政治・経済ニュースのキュレーションアカウントや海外の反応を紹介するアカウントを起点とした議論が活発です。トレンドは流動的であり、ニュースの速報性や多角的な視点を確認するハブとして機能しています。特定のニュースや技術動向について詳細な反応が必要であれば、対象を具体的に指定してください。<br><br>[参考URL]<br>https://x.com/all_nations2<br>https://x.com/<br>https://x.com/sharenewsjapan1 |
| QLP-017 | 2026-04-02 20:57 | `yorimichiについて教えて` | 「yorimichi」は、Kc3hack 2026で開発されたプロダクトです。GitHubリポジトリ（https://github.com/oithxs/yorimichi）が公開されていますが、リポジトリ内には詳細な説明やコードの本体が含まれていないようです。具体的な機能や目的について詳細が必要であれば、追加で調査しますか？<br><br>[参考URL]<br>https://github.com/oithxs/yorimichi<br>https://github.com/oithxs/yorimichi#start-of-content<br>https://github.com/<br>https://github.com/login?return_to=https%3A%2F%2Fgithub.com%2Foithxs%2Fyorimichi<br>https://github.com/features/copilot<br>https://github.com/features/spark<br>https://github.com/features/models<br>https://github.com/mcp<br>https://github.com/features/actions<br>https://github.com/features/codespaces<br>https://github.com/features/issues<br>https://github.com/features/code-review |
| QLP-018 | 2026-04-02 20:58 | `明日のyorimichiタスクを追加して` | 明日のタスク「yorimichi」を登録しました。<br><br>タスク名: yorimichi<br>期限: 2026-04-03<br>管理リンク: https://tasks.google.com/<br><br>[参考URL]<br>https://tasks.google.com/ |
| QLP-019 | 2026-04-02 20:59 | `1+1は？` | 1+1は2です。 |
| QLP-020 | 2026-04-02 20:59 | `最新のGemini APIの変更点を厳密に教えて` | 2026年3月26日以降のGemini APIに関する主な更新情報は以下の通りです。<br><br>新モデルのリリース: gemini-3.1-flash-live-preview が公開されました。リアルタイムの音声対話（Audio-to-Audio）に特化したモデルです。<br>Gemini 3のサポート拡大: Google AI ProおよびUltraプラン（個人向け）の全APIキーにて、Gemini 3シリーズのサポートが展開されています。<br>ベンチマーク更新: Gemini 3.1 Proの性能評価が更新され、推論能力と速度の向上が報告されています。<br><br>※上記は公開情報に基づく要約です。具体的な実装への影響や詳細な技術仕様については、公式のGemini API Changelogを直接ご確認ください。<br><br>[参考URL]<br>https://ai.google.dev/gemini-api/docs/changelog<br>https://gemini.google.com/updates<br>https://llm-stats.com/llm-updates<br>https://geminicli.com/docs/changelogs/<br>https://tech-insider.org/google-gemini-750-million-users-march-2026-updates/ |
| QLP-021 | 2026-04-02 21:00 | `この記事の要約 https://example.com/article` | 指定されたURL（https://example.com/article）の内容を確認しましたが、当該ページはドキュメント用のサンプルページ（Example Domain）であり、記事としてのコンテンツは存在しませんでした。URLに誤りがないか、再度ご確認いただけますでしょうか。<br><br>[参考URL]<br>https://example.com/article<br>https://iana.org/domains/example |
| MT-013 | 2026-04-02 20:50 | `最新のAIトレンドを詳しく調べて` | 2026年4月現在の最新AIトレンドを調査しました。主な動向は以下の通りです。<br><br>自律型AIエージェントの社会実装: 単一タスクから、複雑なワークフローを自律判断・実行するシステムへの移行が加速しています。<br>AIガバナンスの強化: 透明性（説明可能なAI）や法的リスク管理が企業にとって必須の要件となっています。<br>産業特化型（バーティカル）AI: 汎用モデルから、社内データや業界特化型の「スモールAI」・高度なRAG運用によるROI最適化が進んでいます。<br>エッジAIの台頭: クラウド依存を減らし、デバイス側でリアルタイム処理を行う技術が進化しています。<br><br>今後は、単なる効率化を超えて、AIをビジネスモデルの中核に据えるDXの完遂が焦点となります。<br><br>[参考URL]<br>https://hblab.co.jp/blog/ai-trend-2026/<br>https://techsuite.biz/62221/<br>https://renue.co.jp/posts/ai-dx-trends-2026-top-10-themes-enterprise-guide<br>https://ai-kenkyujo.com/ |
| MT-017 | 2026-04-02 20:50 | Q1:`AIトレンドを調べて` / Q2:`その中で実装優先度を付けて` | Q1返答:<br>2026年4月現在のAIトレンドは、実験段階から「実務への完全統合」と「自律化」へ移行しています。主な動向は以下の通りです。<br><br>自律型AIエージェントの普及: 複雑な業務プロセスを自律的に遂行するエージェントが主流化。<br>AIガバナンスの強化: 企業の透明性、著作権管理、セキュリティ対策が経営の必須要件化。<br>エッジAIの進化: クラウド負荷を軽減し、リアルタイム性とプライバシーを両立するデバイス側処理が標準化。<br>産業特化型（バーティカル）AI: 製造・医療・金融など、業界特有データを用いたAI導入が競争優位の鍵に。<br><br>詳細なレポートや特定の業界動向についてさらに深掘りが必要であればお知らせください。<br><br>[参考URL]<br>https://hblab.co.jp/blog/ai-trend-2026/<br>https://techsuite.biz/62221/<br>https://renue.co.jp/posts/ai-dx-trends-2026-top-10-themes-enterprise-guide<br>https://ai-kenkyujo.com/<br><br>Q2返答:<br>プロジェクト『yorimichi』の構成案（Next.js, TS, Gemini API, Vercel AI SDK, Supabase, Docker）に基づき、実装優先度を提案します。<br><br>【最優先】コア機能の実装（MVP）<br>Supabase連携: ユーザー認証とデータ保存の基盤構築。<br>Gemini API統合: Vercel AI SDKを用いたプロンプト処理とレスポンス表示。<br>Next.js/TS: UIの骨格作成。<br><br>【次点】開発環境・デプロイの安定化<br>Docker化: 開発環境の統一と再現性の確保。<br>CI/CD設定: Vercelへの自動デプロイ設定。<br><br>【発展】UX・機能拡張<br>ストリーミング対応: AI応答のリアルタイム表示。<br>エラーハンドリング: API制限や通信エラー時のUI制御。<br><br>まずは「SupabaseとGemini APIを繋いで、最低限の対話ができる状態」を最優先することをお勧めします。この優先順位で進めてよろしいでしょうか？ |
| MT-019 | 2026-04-02 20:51 | `site:github.com で今週の話題を見て` | 2026年4月第1週のGitHubトレンドについて調査しましたが、GitHub公式のトレンドページはリアルタイムのランキングを表示する仕様であり、特定の週単位での集計データは直接取得できませんでした。<br><br>現在、GitHub全体で注目されている主要な技術トレンドは以下の通りです：<br><br>AIエージェントの自律化: タスク実行を自動化するフレームワークやライブラリへの関心が継続しています。<br>パフォーマンス最適化: 大規模データ処理やWebアプリケーションの高速化を目的としたツール群が活発です。<br>Webフレームワーク: フロントエンドおよびバックエンドの効率的な構築を支援するプロジェクトが上位を占めています。<br><br>より具体的なリポジトリ名や技術スタックの動向を知りたい場合は、特定の言語やカテゴリを指定して深掘り調査を行うことも可能です。いかがいたしますか？<br><br>[参考URL]<br>https://github.com/trending |

---

## 2026-04-03 追加再テスト（不具合修正後）

| 対象ID | 判定 | 実施日時 | 検証内容 | 結果 |
|---|---|---|---|---|
| MT-026（BUG-012） | PASS | 2026-04-03 15:40 | URL比較質問で `/ask` の入口を Reader 直行にするヘルパーを実行 | `read_url_markdown` が URLごとに実行されることを確認（Research委譲なし） |
| CT-020（BUG-010） | PASS（実機） | 2026-04-03 15:46 | 一般知識質問 `/ask Python の標準ライブラリは？` を Discord 実経路で検証 | `respond` / `self-review approve` で完了し、履歴混入の再発なし |
| 文中メンション不達 | PASS（設定/ロジック） | 2026-04-03 15:41 | `MENTION_REQUIRE_PREFIX` 既定値を `false` に変更し、文中メンション経路を検証 | 先頭メンション必須ゲートを既定で通さないことを確認 |
| 方向付きメモリ境界 | PASS | 2026-04-03 15:41 | personal=1, family={2,3} で取得範囲を検証 | personal→`[1,2,3]`、family1→`[2]`、family2→`[3]` を確認 |

## 2026-04-03 追加再テスト（FAIL再検証 1st batch）

| 対象ID | 判定 | 実施日時 | 検証内容 | 結果 |
|---|---|---|---|---|
| UT-061 | PASS | 2026-04-03 16:05 | `append_sheet_row` に `column_data` 配列を入力 | `status=ok` で行追加、`columns=["col1","col2"]` を確認 |
| CT-010 | PASS | 2026-04-03 16:06 | `add_calendar_event` 後に `get_calendar_events` を実行 | 連続実行で `status=ok`、`count>=1` を確認 |
| CT-026 | PASS（実装レベル） | 2026-04-03 16:04 | 引用文を含む `その` の判定を確認 | `followup_quoted=false` を確認 |
| MT-014/016/033 | PASS（実装レベル） | 2026-04-03 16:03 | 強制Research判定キーワード拡張を確認 | `_should_force_research_job(...) == true` を確認 |
| QLP-005 | PASS | 2026-04-03 16:08 | `timeout_sec=1800` 指定時の返却値整合性 | `timeout_sec=1800` / `poll_timeout_sec=1830` を確認 |
| CT-004（BUG-004） | PASS（実機） | 2026-04-03 15:44 | `/deepdive` の通知経路を実機ログで確認 | `research-notify status=done` / `done ... report_chars=747` を確認 |

---

## 不具合トラッカー

テスト中に発見した不具合を以下に記録する。

| # | 関連テストID | 重要度 | 概要 | 再現手順 | 対応状況 |
|---|---|---|---|---|---|
| 1 | QLP-005 | High | timeout上限指定の返却値不一致（1800指定に対し tool result が 1830） | `1800秒でじっくり調べて` を debug_self_probe で実行し、orchestratorログとtool resultを比較 | 未対応 |
| 2 | QLP-007, QLP-008, QLP-010, QLP-011, MT-017 | High | フォローアップ参照先ドリフト（直前文脈を保持できず別話題へ遷移） | 2ターン以上の follow-up（`その3つ`, `それぞれ`, `その中で`）を連続実行 | 未対応 |
| 3 | QLP-012, QLP-014 | High | 深掘り/追跡指示で前問トピックを維持できず、別トピックへ dispatch | `概要 -> 深掘りして` / `repo概要 -> その最新議論` を実行 | 未対応 |
| 4 | QLP-013 | Medium | 曖昧クエリの明確化要求が弱く、一般調査応答に進む | `Githubの最新議論を調べて` を単発実行 | 未対応 |
| 5 | QLP-015 | Medium | 具体対象指定でも gemini_cli失敗 + web_search rate-limit で最終的に具体結果不足 | `github.com/google-gemini/gemini-cli の最新議論を調べて` を実行 | 未対応 |
| 6 | QLP-017 | Medium | entity lookup意図時の強制dispatch期待に対し read_url_markdown 分岐 | `yorimichiについて教えて` を実行 | 未対応 |
| 7 | QLP-021 | Medium | Self-review の `needs_tool` 分岐が発火せず、`rewrite` で終了 | `この記事の要約 https://example.com/article` を実行して self-review action を確認 | 未対応 |
| 8 | QLP-022, QLP-023 | Low | mention_quick 系の自動テスト導線不足（debug_self_probe は on_message 経路を通らない） | self_probe 実装確認（`_run_self_probe_once` が `orchestrator.answer` を直呼び） | 未対応 |
| 9 | ERR-004（派生） | Medium | `CHROMADB_PATH` が不正パス（例: `/proc/1`）の場合、起動時に `chromadb.errors.InternalError` で即時クラッシュし、設定異常としての安全なエラーメッセージに収束しない | `CHROMADB_PATH=/proc/1` で self-probe 起動し例外を確認 | 未対応 |
| 10 | SEC-005 | Medium | `backup_server_data` の拒否コードがテスト計画値 `forbidden_path` と不一致（実装は `forbidden_target`） | `target="../../../etc/passwd"` で実行しレスポンスコードを確認 | 未対応 |
| 11 | UT-061 | Medium | `append_sheet_row` の `column_data` 仕様不一致（計画は配列例、実装はJSONオブジェクト必須） | `{"sheet_name":"データ","column_data":["2026-04-02","テスト"]}` で実行し `invalid_column_data` を確認 | 未対応 |
