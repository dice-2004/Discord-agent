# Change Log

このファイルを本プロジェクトの正式な変更履歴として運用します。

## 2026-04-01

## 2026-04-02

### テスト項目書更新

- `docs/TEST_PLAN.md` にエラーハンドリング・耐障害性テスト（§7: ERR-001〜ERR-010）を追加した
- Discord質問ロジックパス網羅テスト（§8: QLP-001〜QLP-023）を追加した。Research Controls 注入、Recent Conversation 文脈組立、フォローアップ解決・指示語注入、曖昧クエリ検出・拒否、強制ディスパッチ、Self-Review、メンション高速カレンダー/タスクの全パスをカバー
- セキュリティテスト（§9: SEC-001〜SEC-006）、設定バリデーションテスト（§10: CFG-001〜CFG-008）、エッジケース・境界値テスト（§11: EDGE-001〜EDGE-012）を追加した
- テスト実施手順（§5.1）に新セクション（§7〜§11）の実施順序を追記した
- `docs/TEST_RESULTS.md` を新規作成。全192件のテスト結果記録ファイルとして、ID・判定・実行ルート・使用ツール/エージェント・観測結果を記録する構造を定義した


### 仕様更新

- Research Agent の `mode=fallback` を「Gemini CLIを使わず、管理AI（Gemini API Orchestrator）を優先するモード」として明確化した
- `mode=auto` は「Gemini CLI先行 + 必要時に管理AIを追加」の方針を明確化し、管理AI失敗時のみ `source_deep_dive` へフォールバックする仕様に整理した

### 実装

- `src/research_agent/research_agent_server.py` の実行フローを更新し、`mode=fallback` で Orchestrator を優先実行するようにした
- Research Job 完了時に `decision_log` を SQLite へ保存して API レスポンスで返せるようにした
- `src/tools/deep_dive_tools.py` と `src/main_agent/tools/deep_dive_tools.py` にクエリ重複除去を追加し、同一テーマの重複検索を抑制した
- `RESEARCH_AGENT_GEMINI_MODEL` を導入し、Gemini CLI 起動時に `--model` を明示指定することで Manual 設定依存を回避した
- ツール実装を `src/tools/` へ完全集約し、`src/main_agent/tools/` の重複実装を削除した
- Dockerfile を `docker/` 配下へ移設し、`docker-compose.yml` の build 参照先を更新した

### 仕様更新

- Main Agentの実装ディレクトリ表記を `src/main_agent/` へ更新した（旧 `src/discord_ai_agent/` は互換shimとして残置）
- Research Agentを段階実装対象へ更新し、別コンテナ + 軽量HTTP + 共有トークンの最小通信方式を仕様に追記した
- Gemini CLI配置方針を追記し、Research Agentコンテナ同梱を既定推奨、ホスト実行を代替案として明文化した
- n8n中継方式を非推奨化し、外部アクションをBotコード内で直接実行する方針へ更新した
- `/n8n_action` 中心の運用方針を `/action` 中心へ更新し、`/n8n_action` は互換コマンドとして扱う方針へ変更した
- `.env` 仕様を `N8N_*` 系から `INTERNAL_*` 系へ移行し、認証誘導URL（`GITHUB_AUTH_URL` / `SMTP_AUTH_URL`）を追加した
- フェーズ分割前提を廃止し、全機能を設計対象とする統一方針へ更新した
- 本運用必須4要件（キュー制御・チェックポイント・JSON強制・疑似成功禁止）を追加した
- 未実装機能の扱いを `not_implemented_action` エラー返却へ統一する方針を追加した
- `.env` 仕様へ `MAX_CONCURRENT_HEAVY_TASKS` / `HEAVY_TASK_TIMEOUT_SEC` / `CHECKPOINT_DB_PATH` を追加した
- `docs/DESIGN.md` から Phase 前提の記述を外し、継続実装前提の文書へ再編
- Tool章を「4本柱」から「6本柱」へ更新し、将来実装枠として「Discord過去ログ検索ツール」「n8n Webhookツール」を追加
- データベース設計章へ将来拡張枠として「カスタムドキュメントRAG（PDF/テキスト）」「SQLite（権限/承認待ち管理）」を追加
- 0.3 の非実装項目へ、n8n/Discord過去ログ検索/カスタムドキュメント/SQLite を明記
- メモリ受け入れ条件を現行実装に合わせ、検索スコープ制御可能な要件へ更新
- ロードマップ章をフェーズ分割から継続実装ロードマップへ更新
- Discord運用仕様へ `/logsearch`（scope=channel/guild、明示キーワード検索）を追記
- `.env` 仕様へ `LOGSEARCH_DEFAULT_SCOPE` を追記
- 第二の自分化要件として、長期ペルソナ記憶（性格/好み/長期目標/定常タスク）を仕様へ追加
- メモリ要件へ、同意ポリシー（opt-in）・削除権・説明可能性・`user_id`分離要件を追記
- Discord運用仕様へ `/profile_show` `/profile_set` `/profile_forget` の任意追加方針を追記
- `.env` 仕様へ `PERSONA_MEMORY_*` 系設定（有効化/確認フラグ/件数上限/コレクション名/証跡件数）を追記
- `/logsearch` の結果表示に簡易スコア（一致度/新しさ）を追加できる仕様を追記
- `/runcli_audit` のevent種別フィルタ仕様を追記
- `/n8n_action` のaction別必須キー検証仕様を追記
- n8n標準インターフェースとして `execute_n8n_workflow`（`action` + `parameters`）のJSON仕様を追記
- n8nアクション定義を `add_calendar_event` / `get_calendar_events` / `send_email` へ整理し、必須キーを明記
- Gemini Web会話エクスポート取り込み方針（対応形式: json/csv、mpegは文字起こし前提）を追記
- 運用用 `.env` に許可ギルド3件（個人1 + 身内2）と n8n関連設定を反映
- `docker-compose.yml` に n8nサービス（ローカルバインド/永続化/Basic Auth）を追加
- `README.md` に n8n構築手順、Webhook保護、Google Calendar OAuth設定手順を追加
- `.env.example` を n8nセルフホスト運用の設定項目に合わせて更新
- 本番環境でもn8n設定思想を維持しつつ、本番専用シークレット/URLへ切替する運用方針を明文化
- n8n初心者向けに、画面クリック手順を含む本番導入ガイド `docs/N8N_DEPLOYMENT_BEGINNER_GUIDE.md` を追加
- n8n初心者向けガイドに「最短手順」を追記し、入力キーとクリック順のみで実施できる形式へ簡略化
- n8n作業の要否を先に判断できる分岐（必要/不要）をガイドへ追加
- 開発用/本番用のコピペ可能な `.env` 完成形サンプルをガイドへ追加
- n8n運用ガイドに、GUIを使わない完全CUI手順（list/publish/restart/export/curl）を追記
- 方向付きメモリ境界（`DIRECTIONAL_MEMORY_ENABLED` / `PERSONAL_GUILD_ID` / `FAMILY_GUILD_IDS`）の仕様を追加
- `/ask` を入口にAIが自律ツール選択する運用方針へ合わせ、ask限定モード表現を撤回
- n8nアクション定義を拡張（`add_notion_memo` / `append_sheet_row` / `create_github_issue` / `send_line_notification` / `backup_server_data`）
- メッセージ連携要件を LINE 通知から Slack メッセージ取得へ切替し、`get_slack_messages`（必須: `channel_id`, `limit`）を反映
- Slack取得にはBot/Appトークンが実質必須のため、運用方針に合わせて `get_slack_messages` を見送り `send_email` に戻した

### 実装

- 互換shimを整理し、`src/discord_ai_agent/` と `src/main_agent/research_agent_server.py` を削除して、Research実体を `src/research_agent/research_agent_server.py` に一本化した
- `deepdive` のResearch投入時に `job_id` / `topic` / `source` を `research_audit.jsonl` へ監査記録するようにした
- Research完了通知を改善し、長文レポートは要約メッセージ + `research_report.txt` 添付へ切替えるようにした
- docker compose のMainサービス名/コンテナ名を `main-agent` へ変更した
- 実装パッケージを `src/main_agent/` へ移行し、`src/discord_ai_agent/` は後方互換用の薄いshimへ変更した
- `research-agent` サービスを `docker-compose.yml` に追加し、Main/Researchの別コンテナ分離を実装した
- `src/discord_ai_agent/research_agent_server.py` を追加し、`POST /v1/jobs` / `GET /v1/jobs/{job_id}` とSQLiteジョブ状態管理を実装した
- `dispatch_research_job` ツールを追加し、Main AgentからResearch Agentへジョブ投入・ポーリング取得できるようにした
- `get_research_job_status` ツールを追加し、Main AgentからResearchジョブ状態を参照できるようにした
- `deepdive` コマンドに Research Agent 経由モード（`DEEPDIVE_USE_RESEARCH_AGENT=true`）を追加した
- `deepdive` のResearch経由時を非同期投入に変更し、完了/失敗を同一チャンネルへ自動通知するバックグラウンドポーラーを実装した
- Main起動時に `research_job` チェックポイント（queued）から通知ポーラーを再開する処理を追加した
- `.env.example` に `RESEARCH_AGENT_*` / `DEEPDIVE_USE_RESEARCH_AGENT` 設定を追加した
- `/n8n_action` 互換コマンドを削除し、`/action` のみを正式コマンドとして運用するようにした
- `/action` を `debug_action`（デバッグ専用）へ移行し、通常運用は `/ask` 中心とする方針へ更新した
- `/auth_status` コマンドを追加し、外部連携の認証状態と導線URLをDiscord上で確認できるようにした
- 起動時にグローバル/ギルドのコマンド再同期前に clear を実行し、旧コマンドの残留を自動クリーンアップするようにした
- `backup_server_data` の内部アクション実装を追加し、許可ルート配下のみを `.tar.gz` へバックアップできるようにした
- `append_sheet_row` の内部アクション実装を追加し、`SHEET_STORAGE_DIR` 配下のCSVへ行追加できるようにした
- `add_notion_memo` の内部アクション実装を追加し、`NOTION_MEMO_STORAGE_PATH` へJSON Lines形式で記録できるようにした
- `execute_internal_action` ツールを追加し、Webhook中継なしで action をコード内実行する方式へ移行した
- メンション高速カレンダールーターで和文日付（例: `2026年4月7日`）を解釈できるようにし、定型入力の取りこぼしを削減した
- メンション高速カレンダールーターで終日表現（`終日` / `全日` / `一日中`）を `00:00-23:59` として処理できるようにした
- メンション高速カレンダールーターで「内容/日時」形式の箇条書き文（追加キーワードなし）も予定追加意図として判定できるようにした
- `add_calendar_event` / `get_calendar_events` の日時パーサーを強化し、ISO8601に加えて和文・簡易日付時刻フォーマットを受理するよう改善した
- 内部アクション日時でタイムゾーン未指定時は `TZ`（既定: `Asia/Tokyo`）を補完するようにし、時刻形式エラーの発生率を低減した
- `add_calendar_event` に終日イベントのネイティブ登録（`all_day=true`, `date`, `end_date`）を追加し、00:00-23:59擬似登録に依存しない運用へ改善した
- メンション高速ルーターで `00:00-23:59` 指定を終日として自動解釈する補正を追加した
- オーケストレーター方針文を更新し、終日指定時に時刻確認を要求せず `all_day + date` 形式で実行するよう明示した
- `execute_internal_action` に action別名の正規化を追加し、`calendar_add_event` / `calendar_get_events` を `add_calendar_event` / `get_calendar_events` へ自動変換できるようにした
- メンション高速ルーターで `4月5日` のような和文月日入力を解釈できるようにし、終日登録の取りこぼしを低減した
- オーケストレーター方針文を更新し、入力が明確な予定追加は確認質問を省略して実行するルールを強化した
- `add_calendar_event` のpayload正規化を追加し、`summary` / `event` / `name` などの同義キーから `title` を補完できるようにした
- `docs/DESIGN.md` を更新し、Research Agent/Eternal Explorer の別コンテナ分離推奨方針を追記した
- `docs/DESIGN.md` と `.env.example` の `INTERNAL_ACTION_REQUIRED_FIELDS` を更新し、`add_calendar_event` は `title` を共通必須として timed/all-day の二方式を許可した
- `create_github_issue` と `send_email` の内部アクション実装を追加し、認証未設定時は `auth_required` と `auth_url` を返すようにした
- `docker-compose.yml` から n8n サービスを外し、Bot単体 + runtime永続ボリューム構成へ変更した
- `docs/N8N_DEPLOYMENT_BEGINNER_GUIDE.md` を非推奨ガイドへ更新し、現行運用導線を `README.md` / `docs/DESIGN.md` へ統一した
- オーケストレーターに重い処理の同時実行制限（`asyncio.Semaphore`）を追加し、`/ask` と重いツール実行をキュー制御下へ移行した
- オーケストレーターへ SQLite ベースのチェックポイント保存API（save/load/list）を追加した
- Gemini意思決定系の呼び出しで `response_mime_type=application/json` を指定し、JSONパース安定性を強化した
- n8n未実装アクションの `stub-success` 応答を廃止し、`not_implemented_action` + HTTP 501 を返すよう変更した
- `create_github_issue` アクションを n8n 側で stub 応答から実API呼び出しへ更新し、`GITHUB_TOKEN` 未設定時は 503 を返す安全分岐を追加した
- n8n の `create_github_issue` で GitHub API 応答を判定し、成功時（201）と失敗時（4xx/5xx）でJSONレスポンスを分離した
- `trigger_n8n_webhook` に再試行設定（`N8N_RETRY_COUNT` / `N8N_RETRY_BACKOFF_SEC`）を追加し、HTTPエラー時のデバッグ情報を強化した
- `/logsearch` コマンドを追加し、Discord上で過去ログ候補を明示検索できるようにした
- `/runcli_audit` コマンドを追加し、runcli監査ログ（JSON Lines）の末尾イベントをephemeral表示できるようにした
- `.env` 仕様へ `RUNCLI_AUDIT_TAIL_DEFAULT` を追加した
- `trigger_n8n_webhook` ツールを追加し、許可済みactionのみn8n webhookへJSON POSTできるようにした
- `/n8n_action` コマンドを追加し、Discordからn8n actionをephemeralで実行できるようにした
- `.env` 仕様へ `N8N_WEBHOOK_BASE_URL` / `N8N_ALLOWED_ACTIONS` / `N8N_WEBHOOK_TOKEN` / `N8N_TIMEOUT_SEC` を追加した
- `/logsearch` に一致度/新しさスコア表示を追加した（設定で無効化可能）
- `/runcli_audit` にevent種別フィルタ（all/requested/approved/rejected/executed/...）を追加した
- `trigger_n8n_webhook` に `N8N_ACTION_REQUIRED_FIELDS` ベースの必須キー検証を追加した
- `ChannelMemoryStore` にユーザー単位ペルソナ記憶のCRUD（set/get/forget）を追加した
- `/profile_show` `/profile_set` `/profile_forget` を追加し、ユーザープロファイルをDiscordから管理可能にした
- 回答生成時に `PERSONA_MEMORY_INCLUDE_IN_PROMPT=true` ならユーザープロファイルをシステム文脈へ注入するようにした
- `.env.example` に `PERSONA_MEMORY_*` 設定群を追加した
- 方向付きメモリ境界設定（`DIRECTIONAL_MEMORY_ENABLED` / `PERSONAL_GUILD_ID` / `FAMILY_GUILD_IDS`）を追加し、個人サーバーからのみ身内サーバー記憶を参照できるポリシーを実装した
- n8nの `execute_n8n_workflow` をCUIで再現可能にするテンプレート `n8n/workflows/execute_n8n_workflow.json` を追加した
- `ASK_ONLY_MODE` による補助コマンド遮断を撤回し、`/ask` 入口からの自律ツール利用モデルへ戻した
- n8n webhook呼び出しを `action + parameters` 形式の単一エンドポイント（`execute_n8n_workflow`）へ統一した

## 2026-03-31

### 仕様策定・明確化

- `add_calendar_event` / `get_calendar_events` の内部アクション実装を追加し、`CALENDAR_EVENTS_STORAGE_PATH` へJSON Lines形式で記録・期間検索できるようにした
- initial_profileの欠損時挙動、サイズ上限、推奨テンプレートを追加
- Web検索の件数、タイムアウト、再試行、出力フォーマットを固定
- ChromaDBの分離キー、命名規則、保存スキーマ、検索件数を固定
- /ask の最小仕様、ギルド制御、長文応答分割、添付フォールバックを確定
- ログ方針と .env 推奨設定を確定
- Google GroundingはPhase 1では既定不採用、Phase 2候補に整理
- 開発停滞回避のため、フェーズ制限を緩和する「実装優先モード」を追加

### 実装

- Discord BotのPhase 1実装を追加
- Main Agent、Orchestrator、Memory、Search Toolを実装
- `ask_response.txt` 添付方針を実装
- 同一ドメイン重複除外の検索整形を実装
- Gemini連携をGoogle公式SDK直呼び出しへ移行（LangChain依存を削減）
- Gemini tool callingの`thought_signature`問題を回避するため、Orchestrator側のツール判定・実行方式へ変更
- `search_tools.py` からLangChain `@tool` デコレータ依存を除去し、独立関数として再実装
- Orchestratorに自律検索計画を追加（最大3クエリ、最新性判定、結論先出しポリシー）
- 天気・ニュース・価格系質問で補助クエリを自動生成し、回答の具体性を向上
- 未使用の `src/discord_ai_agent/core/orchestrator_v2.py` を削除し、実行対象を `orchestrator.py` に一本化
- Orchestratorの検索計画を汎用化し、LLMによる自律クエリ生成（JSON計画）を追加
- 検索計画失敗時はヒューリスティックへフォールバックする二段構えに改善
- ツールレジストリを導入し、`web_search` / `read_url_markdown` / `source_deep_dive` / `run_local_cli` を実装
- オーケストレーターを汎用の自律ツールループ方式へ更新（各ターンで tool または respond を選択）
- CLIツールにHitL相当の安全策（承認トークン + 許可コマンド制限）を追加
- DuckDuckGoレート制限対策として、検索キャッシュ（TTL）とクールダウン制御を追加
- 深掘りツールでレート制限検知時に残りクエリを早期停止する制御を追加
- ツールレジストリに引数バリデーション/正規化を追加し、LLMの誤ったツール引数を実行前に遮断
- オーケストレーターに自己評価ループ（approve/rewrite/needs_tool）を追加し、回答前に品質検査を実施
- エージェント意思決定ログ（turn/action/tool/reason）とツール結果サマリログを追加
- `/ask` 実行時に直近チャンネル履歴をバックフィルし、Bot導入前メッセージをメモリ化する処理を追加
- メモリ保存を `add` から `upsert` に変更し、Discord `message_id` をキーに重複保存を抑制
- メモリ検索をハイブリッド化（語彙重なり優先 + 新しさフォールバック）し、想起精度を改善
- 履歴取り込みを `/ask` 同期フェッチ方式から、起動時バックフィル + `on_message` 常時収集方式へ変更
- 取り込みカーソルを `data/chromadb/memory_ingest_cursor.json` に永続化し、再起動時は差分のみ履歴同期するよう改善
- Message Content Intentが未許可環境でクラッシュしないよう、`DISCORD_ENABLE_MESSAGE_CONTENT_INTENT` を追加（既定false）
- Intent無効時は全量履歴取り込みをスキップし、起動を優先するフェイルセーフを追加
- メモリ保存をチャンネル単位に加えてギルド全体インデックスへも同時保存し、サーバ全体の想起に対応
- 回答時メモリ参照を既定で `MEMORY_RETRIEVAL_SCOPE=guild` に変更し、別チャンネル過去会話も参照可能に改善
- 起動時バックフィル対象をテキストチャンネルに加えてアクティブスレッドへ拡張
- `MEMORY_BOOTSTRAP_FORCE_REINDEX` を追加し、既存履歴の再インデックスを差分カーソルを無視して実行可能にした
- メモリ検索を `collection.get(limit=...)` 依存から `collection.query(...)` ベースへ変更し、古い履歴の取りこぼしを低減
- メモリ取得ログにヒット件数と参照チャンネル一覧を追加し、クロスチャンネル参照可否を運用で検証しやすく改善
- メモリ保存時にDiscordメッセージの作成時刻を保持するよう変更し、バックフィル後の日時ズレを軽減
- 起動時バックフィル対象をアーカイブ済みスレッドまで拡張し、長期間の会話取りこぼしを低減
- 1回限りの全再インデックス運用（`MEMORY_BOOTSTRAP_FORCE_REINDEX=true`）を実施し、ギルド履歴の再同期を確認
- 回答末尾に参照メモリ出典を付与できる `MEMORY_RESPONSE_INCLUDE_EVIDENCE` / `MEMORY_RESPONSE_EVIDENCE_ITEMS` を追加
- 「過去ログ参照権限がない」系の既知テンプレート回答をメモリ再ランク時に減点し、誤った再提示を抑制
- 参照メモリ出典の表示で同一内容の重複行を除外し、チャンネル名を優先表示するよう改善
- URL単体投稿・極短文・区切り線投稿に品質減点を適用し、メモリ検索の文脈適合率を向上
- メモリ検索の最終採用で同一チャンネル・同一内容の重複候補を除外し、文脈ノイズを低減
- `MENTION_ASK_ENABLED` を追加し、Botメンション経由の質問応答ルートを実装（/askと同等の応答生成を利用）
- `/memory_status` コマンドを追加し、Discord上からギルドメモリの保存件数と主要コレクション件数を確認可能にした
- `/runcli` コマンドを追加し、Discordボタン（承認/拒否）経由で許可済みCLIを実行できるHitL運用を実装
- `/runcli` 承認処理で必要な `asyncio` import漏れを修正
- 参照メモリ出典の日時表示をJSTへ統一し、運用時の時刻解釈のズレを軽減
- `/runcli` 承認ボタンをephemeral表示から通常表示へ変更し、複数承認者でのHitL運用を成立させた
- `/runcli` の監査ログ出力を追加し、request/approve/reject/execute（exit code・結果プレビュー）をJSON Linesで永続化
- `/readurl` コマンドを追加し、Readerツールの単体検証をDiscord上で実行可能にした
- `/deepdive` コマンドを追加し、source指定（auto/github/reddit/youtube/x）で深掘りツールを直接検証可能にした
- `.env.example` に `RUNCLI_AUDIT_LOG_PATH`、検索キャッシュ制御（`SEARCH_CACHE_TTL_SEC` / `SEARCH_COOLDOWN_SEC`）を追記

### 運用基盤

- Dockerfile と docker-compose.yml を整備
- ルート基準のsrcレイアウトへ再編
- `.env.example` を追加し、`.env` はGit管理外へ変更
- `data/chromadb` 永続化前提の構成へ変更

### ドキュメント

- DESIGNにDocker必須運用を反映
- DESIGNのパス記述をsrcレイアウトに更新
- READMEにDiscord Botセットアップ手順を追加
- READMEのn8n章から本番導入ガイドへの参照導線を追加
- READMEへn8n workflowテンプレートのCUI import手順を追加
- change logの正式記録先を本ファイルに統一
