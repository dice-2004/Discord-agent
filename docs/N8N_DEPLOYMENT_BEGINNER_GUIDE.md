# n8n本番導入ガイド（初心者向け）

## 最短手順（まずこれだけ）

以下だけ実施すれば、開発から本番へ移行できます。

### 0. 先に判断（n8n作業が必要か）

- 必要: カレンダー追加・予定取得・メール送信をBotから実行したい
- 不要: Botを質問応答だけで使う（外部サービスへの実行を使わない）

不要の場合は、n8nの画面作業は不要です。Botはそのまま質問応答に使えます。

### 1. 本番サーバーの .env に入力

次のキーを本番用の値で埋める。

```env
N8N_BASIC_AUTH_USER=<本番の管理ユーザー名>
N8N_BASIC_AUTH_PASSWORD=<本番の強いパスワード>
N8N_ENCRYPTION_KEY=<32文字以上のランダム文字列>
N8N_WEBHOOK_TOKEN=<長いランダム文字列>
N8N_PROTOCOL=https
N8N_EDITOR_BASE_URL=https://n8n.example.com
N8N_WEBHOOK_URL=https://n8n.example.com
N8N_WEBHOOK_BASE_URL=https://n8n.example.com/webhook/execute_n8n_workflow
GITHUB_TOKEN=<GitHub Personal Access Token (repo scope)>
```

Bot側 .env も次を同じ値にする。

```env
N8N_WEBHOOK_BASE_URL=https://n8n.example.com/webhook/execute_n8n_workflow
N8N_WEBHOOK_TOKEN=<n8n側と同じトークン>
N8N_ALLOWED_ACTIONS=add_calendar_event,get_calendar_events,add_notion_memo,append_sheet_row,create_github_issue,send_email,backup_server_data
```

### 1.1 コピペ用 .env（開発）

以下は開発用の完成形サンプルです。値はあなたの環境値に置換してください。

DISCORD_TOKEN=<your_discord_bot_token>
GEMINI_API_KEY=<your_gemini_api_key>
LOG_LEVEL=INFO
BOT_GUILD_ID=1228693698632618117
ALLOWED_GUILD_IDS=1228693698632618117,1157172173736267787,1372865580708794418
CHROMADB_PATH=./data/chromadb
MEMORY_BOOTSTRAP_ON_READY=true
MEMORY_BOOTSTRAP_MAX_PER_CHANNEL=0
MEMORY_BOOTSTRAP_BATCH_SIZE=200
INITIAL_PROFILE_PATH=./data/profiles/initial_profile.md
SEARCH_MAX_RESULTS=5
SEARCH_TIMEOUT_SEC=10
READER_TIMEOUT_SEC=12
READER_MAX_CHARS=5000
DEEP_DIVE_MAX_QUERIES=3
GEMINI_TIMEOUT_SEC=30
MAX_DISCORD_MESSAGE_LEN=1900
MAX_TOOL_TURNS=3
MAX_REVIEW_TURNS=1
CLI_ALLOWED_COMMANDS=docker ps,docker compose ps,uptime,df -h,free -m
CLI_APPROVAL_TOKEN=<set_long_random_token>
GEMINI_MODEL=gemini-3.1-flash-lite-preview
DISCORD_ENABLE_MESSAGE_CONTENT_INTENT=true
MEMORY_RETRIEVAL_SCOPE=guild
MEMORY_TOP_K=8
MEMORY_BOOTSTRAP_FORCE_REINDEX=false
PERSONA_MEMORY_ENABLED=true
PERSONA_MEMORY_INCLUDE_IN_PROMPT=true
PERSONA_MEMORY_REQUIRE_CONFIRMATION=true
PERSONA_MEMORY_MAX_FACTS=200
PERSONA_MEMORY_COLLECTION=persona_profiles
PERSONA_MEMORY_EVIDENCE_ITEMS=3
RUNCLI_AUDIT_LOG_PATH=./data/audit/runcli_audit.jsonl
LOGSEARCH_DEFAULT_SCOPE=guild
RUNCLI_AUDIT_TAIL_DEFAULT=20
RUNCLI_AUDIT_EVENT_FILTER_DEFAULT=all
LOGSEARCH_INCLUDE_SCORE=true
LOGSEARCH_SCORE_OVERLAP_WEIGHT=0.7
LOGSEARCH_SCORE_RECENCY_WEIGHT=0.3
N8N_WEBHOOK_BASE_URL=http://n8n:5678/webhook/execute_n8n_workflow
N8N_ALLOWED_ACTIONS=add_calendar_event,get_calendar_events,add_notion_memo,append_sheet_row,create_github_issue,send_email,backup_server_data
N8N_ACTION_REQUIRED_FIELDS=add_calendar_event:title,start_time,end_time;get_calendar_events:time_min,time_max;add_notion_memo:title,content,category;append_sheet_row:sheet_name,column_data;create_github_issue:repository,title,body;send_email:to_address,subject,body;backup_server_data:target
N8N_WEBHOOK_TOKEN=<set_long_random_token>
N8N_TIMEOUT_SEC=12
N8N_RETRY_COUNT=1
N8N_RETRY_BACKOFF_SEC=1
GITHUB_TOKEN=<GitHub PAT with repo scope>
N8N_HOST=0.0.0.0
N8N_PORT=5678
N8N_PROTOCOL=http
N8N_EDITOR_BASE_URL=http://localhost:5678
N8N_WEBHOOK_URL=http://localhost:5678
N8N_BASIC_AUTH_ACTIVE=true
N8N_BASIC_AUTH_USER=admin
N8N_BASIC_AUTH_PASSWORD=<set_strong_password>
N8N_ENCRYPTION_KEY=<32+_char_random_string>
TZ=Asia/Tokyo

### 1.2 コピペ用 .env（本番）

以下は本番用の完成形サンプルです。ドメインと秘密値を本番専用に置換してください。

DISCORD_TOKEN=<your_discord_bot_token>
GEMINI_API_KEY=<your_gemini_api_key>
LOG_LEVEL=INFO
BOT_GUILD_ID=1228693698632618117
ALLOWED_GUILD_IDS=1228693698632618117,1157172173736267787,1372865580708794418
CHROMADB_PATH=./data/chromadb
MEMORY_BOOTSTRAP_ON_READY=true
MEMORY_BOOTSTRAP_MAX_PER_CHANNEL=0
MEMORY_BOOTSTRAP_BATCH_SIZE=200
INITIAL_PROFILE_PATH=./data/profiles/initial_profile.md
SEARCH_MAX_RESULTS=5
SEARCH_TIMEOUT_SEC=10
READER_TIMEOUT_SEC=12
READER_MAX_CHARS=5000
DEEP_DIVE_MAX_QUERIES=3
GEMINI_TIMEOUT_SEC=30
MAX_DISCORD_MESSAGE_LEN=1900
MAX_TOOL_TURNS=3
MAX_REVIEW_TURNS=1
CLI_ALLOWED_COMMANDS=docker ps,docker compose ps,uptime,df -h,free -m
CLI_APPROVAL_TOKEN=<set_long_random_token>
GEMINI_MODEL=gemini-3.1-flash-lite-preview
DISCORD_ENABLE_MESSAGE_CONTENT_INTENT=true
MEMORY_RETRIEVAL_SCOPE=guild
MEMORY_TOP_K=8
MEMORY_BOOTSTRAP_FORCE_REINDEX=false
PERSONA_MEMORY_ENABLED=true
PERSONA_MEMORY_INCLUDE_IN_PROMPT=true
PERSONA_MEMORY_REQUIRE_CONFIRMATION=true
PERSONA_MEMORY_MAX_FACTS=200
PERSONA_MEMORY_COLLECTION=persona_profiles
PERSONA_MEMORY_EVIDENCE_ITEMS=3
RUNCLI_AUDIT_LOG_PATH=./data/audit/runcli_audit.jsonl
LOGSEARCH_DEFAULT_SCOPE=guild
RUNCLI_AUDIT_TAIL_DEFAULT=20
RUNCLI_AUDIT_EVENT_FILTER_DEFAULT=all
LOGSEARCH_INCLUDE_SCORE=true
LOGSEARCH_SCORE_OVERLAP_WEIGHT=0.7
LOGSEARCH_SCORE_RECENCY_WEIGHT=0.3
N8N_WEBHOOK_BASE_URL=https://n8n.example.com/webhook/execute_n8n_workflow
N8N_ALLOWED_ACTIONS=add_calendar_event,get_calendar_events,add_notion_memo,append_sheet_row,create_github_issue,send_email,backup_server_data
N8N_ACTION_REQUIRED_FIELDS=add_calendar_event:title,start_time,end_time;get_calendar_events:time_min,time_max;add_notion_memo:title,content,category;append_sheet_row:sheet_name,column_data;create_github_issue:repository,title,body;send_email:to_address,subject,body;backup_server_data:target
N8N_WEBHOOK_TOKEN=<set_long_random_token>
N8N_TIMEOUT_SEC=12
N8N_RETRY_COUNT=1
N8N_RETRY_BACKOFF_SEC=1
GITHUB_TOKEN=<GitHub PAT with repo scope>
N8N_HOST=0.0.0.0
N8N_PORT=5678
N8N_PROTOCOL=https
N8N_EDITOR_BASE_URL=https://n8n.example.com
N8N_WEBHOOK_URL=https://n8n.example.com
N8N_BASIC_AUTH_ACTIVE=true
N8N_BASIC_AUTH_USER=<prod_admin_user>
N8N_BASIC_AUTH_PASSWORD=<set_strong_password>
N8N_ENCRYPTION_KEY=<32+_char_random_string_fixed>
TZ=Asia/Tokyo

### 2. n8nを起動

```bash
docker compose up -d n8n
docker compose logs -f n8n
```

### 2.1 完全CUI手順（GUIを使わない）

この環境で確認したコマンドだけを記載します。

テンプレートを使って作成する場合:

```bash
docker compose cp n8n/workflows/execute_n8n_workflow.json n8n:/tmp/execute_n8n_workflow.json
docker compose exec -T n8n n8n import:workflow --input=/tmp/execute_n8n_workflow.json
```

1. Workflow一覧を取得

```bash
docker compose exec -T n8n n8n list:workflow
```

2. IDを変数に入れる（名前が `execute_n8n_workflow` のもの）

```bash
WF_ID=$(docker compose exec -T n8n n8n list:workflow | awk -F'|' '$2=="execute_n8n_workflow" {print $1}')
echo "$WF_ID"
```

3. Workflowを有効化

```bash
docker compose exec -T n8n n8n publish:workflow --id="$WF_ID"
docker compose restart n8n
```

4. 有効化を確認

```bash
docker compose exec -T n8n n8n export:workflow --id="$WF_ID"
```

出力JSON内で `"active":true` と `"activeVersionId"` が入っていれば有効です。

5. CUIでWebhook疎通確認

```bash
curl -i -X POST "http://localhost:5678/webhook/execute_n8n_workflow" \
   -H "Content-Type: application/json" \
   -H "x-webhook-token: $N8N_WEBHOOK_TOKEN" \
   -d '{"action":"add_calendar_event","parameters":{"title":"test","start_time":"2026-04-01T10:00:00+09:00","end_time":"2026-04-01T11:00:00+09:00"}}'
```

注記: 現在のWorkflow定義内容によっては `500` が返る場合があります。その場合はノード定義の修正が必要です。

### 3. ブラウザ操作（クリック順）

1. `https://n8n.example.com` を開く
2. Basic Authを入力
3. `Create Workflow` をクリック
4. 画面中央の `+` をクリック
5. `Webhook` を検索して追加
6. 右パネルで
   - `HTTP Method` を `POST`
   - `Path` を `execute_n8n_workflow`
7. Webhookノード右の `+` をクリック
8. `IF` を追加
9. IF条件を `x-webhook-token` = あなたの `N8N_WEBHOOK_TOKEN` に設定
10. IFの true 側に `Switch` を追加
11. 分岐値に `action` を設定
12. ケースを必要数作成
   - `add_calendar_event`
   - `get_calendar_events`
   - `add_notion_memo`
   - `append_sheet_row`
   - `create_github_issue`
   - `send_email`
   - `backup_server_data`
13. 各ケースの末尾に `Respond to Webhook` を追加
14. 右上 `Save` をクリック
15. 右上 `Inactive` をクリックして `Active` にする

### 4. テスト

1. Discordで予定追加系の依頼を送る
2. n8n左メニュー `Executions` をクリック
3. 最新実行を開き、成功/失敗を確認

---

このドキュメントは、次の2点を一気に解決するための手順書です。

- 本番でも開発環境と同様の設定を使うべきか
- n8n初心者でも迷わないように、画面でどこをクリックするか

## 1. 結論

結論として、**本番でも同じ設定思想を使うべき**です。

ただし、以下は「同じ値を使い回す」のではなく、**本番専用の値**に切り替えてください。

- Basic Authのユーザー/パスワード
- Encryption Key
- Webhook Token
- 公開URL（HTTPS）

理由:

- 開発用資格情報の流用は漏えいリスクが高い
- Encryption Keyが不安定だとCredential復号が壊れる
- 本番Webhookは外部から叩かれるため、強い検証が必要

## 2. まず用語を揃える

- 開発環境: あなたのローカルPCや検証用サーバー
- 本番環境: 実際に常用する環境
- n8n UI: ブラウザで開く管理画面
- Workflow: n8nの処理フロー

## 3. 本番移行の全体像

1. 本番サーバーを用意する
2. DNSとHTTPSを準備する
3. 本番用の `.env` を作る
4. n8nを起動して初期ユーザーを作る
5. Workflowを作成/移行する
6. Bot側のWebhook先を本番URLに切り替える
7. テストして監視を開始する

## 4. 本番用 `.env` の必須設定

最低限、次の値を本番専用で設定します。

```env
N8N_BASIC_AUTH_ACTIVE=true
N8N_BASIC_AUTH_USER=<strong-admin-user>
N8N_BASIC_AUTH_PASSWORD=<strong-password>
N8N_ENCRYPTION_KEY=<32+ random chars fixed key>
N8N_WEBHOOK_TOKEN=<long-random-token>
WEBHOOK_URL=https://n8n.example.com/
N8N_HOST=n8n.example.com
N8N_PROTOCOL=https
N8N_PORT=5678
```

注意:

- `N8N_ENCRYPTION_KEY` は本番で固定し、途中で変えないでください
- `WEBHOOK_URL` は必ず HTTPS の公開URLを設定してください

## 5. 初心者向け: n8n画面のクリック手順

以下は「ゼロからWebhook Workflowを作る」手順です。

### 5.1 n8nにログイン

1. ブラウザで n8nのURLを開く（例: `https://n8n.example.com`）
2. Basic Authのポップアップが出たら、ユーザー名とパスワードを入力
3. 初回セットアップ画面が出たら、管理者アカウントを作成

### 5.2 新しいWorkflowを作る

1. 左上または中央の `Create Workflow` をクリック
2. 画面上部のWorkflow名（`Untitled workflow`）をクリックして名前変更
   - 例: `execute_n8n_workflow`

### 5.3 Webhookノードを追加

1. 画面中央の `+` ボタンをクリック
2. 検索欄に `Webhook` と入力
3. `Webhook` ノードをクリックして追加
4. 右側設定パネルで以下を設定
   - `HTTP Method`: `POST`
   - `Path`: `execute_n8n_workflow`

### 5.4 トークン検証ノードを追加（推奨）

1. Webhookノードの右側 `+` をクリック
2. `IF` ノードを追加
3. 条件を次のように設定
   - 左辺: ヘッダ `x-webhook-token`
   - 演算子: `equals`
   - 右辺: 本番の `N8N_WEBHOOK_TOKEN`
4. `false` 側に `Respond to Webhook` ノードを追加し、403相当のJSONを返す

### 5.5 action分岐を作る

1. `IF` の `true` 側に `Switch` ノードを追加
2. 分岐キーを `action` に設定
3. ケースを追加
   - `add_calendar_event`
   - `get_calendar_events`
   - `add_notion_memo`
   - `append_sheet_row`
   - `create_github_issue`
   - `send_email`
   - `backup_server_data`

### 5.6 各actionの処理ノードをつなぐ

例: `add_calendar_event`

1. `Switch` の `add_calendar_event` 分岐先に `Google Calendar` ノードを追加
2. Operationを `Create Event` に設定
3. `title` `start_time` `end_time` を `parameters` からマッピング
4. 最後に `Respond to Webhook` ノードを追加して成功JSONを返す

### 5.7 Workflowを有効化

1. 右上の `Inactive` トグルをクリックして `Active` にする
2. `Save` をクリック

### 5.8 実行履歴の見方

1. 左メニュー `Executions` をクリック
2. 失敗した実行をクリック
3. どのノードで失敗したか確認
4. `Input` と `Output` を見て、受信JSONやエラー文を確認

## 6. Google Calendar連携（クリック手順）

### 6.1 Google Cloud Console側

1. Google Cloud Consoleを開く
2. プロジェクトを作成
3. 左メニュー `APIs & Services` -> `Library`
4. `Google Calendar API` を検索して `Enable`
5. `OAuth consent screen` を開き、同意画面を設定
6. `Credentials` -> `Create Credentials` -> `OAuth client ID`
7. Application typeを `Web application` にする
8. `Authorized redirect URIs` に以下を追加
   - `https://n8n.example.com/rest/oauth2-credential/callback`
9. `Client ID` と `Client Secret` を控える

### 6.2 n8n側

1. Workflow編集画面で `Google Calendar` ノードを開く
2. `Credentials` の `Create new` をクリック
3. `Client ID` / `Client Secret` を入力
4. `Connect my account` をクリック
5. Googleの認可画面で対象アカウントを選択し `許可`
6. n8nに戻って接続済みになっていることを確認

## 7. Bot側切り替え（本番）

Botの `.env` でn8n連携値を本番に切り替えます。

```env
N8N_WEBHOOK_BASE_URL=https://n8n.example.com/webhook/execute_n8n_workflow
N8N_WEBHOOK_TOKEN=<same token as n8n check>
N8N_ALLOWED_ACTIONS=add_calendar_event,get_calendar_events,add_notion_memo,append_sheet_row,create_github_issue,send_email,backup_server_data
```

## 8. 本番運用で追加すべきこと

- 逆プロキシでTLS終端（Nginx/Caddy/Traefik）
- ファイアウォールで不要ポートを閉じる
- n8nデータのバックアップ（volume定期バックアップ）
- 更新手順の標準化（メンテ時間を決めて実施）
- ログ監視と失敗通知

## 9. 最小テスト手順（本番反映直後）

1. Botから1件だけ `add_calendar_event` を実行
2. n8n `Executions` で成功を確認
3. Google Calendarに実際の予定が作成されたか確認
4. 次に `get_calendar_events` を実行
5. 最後に失敗ケース（必須項目欠落）を投げて、エラーハンドリング確認

## 10. よくある初心者つまずき

- `404 Webhook not found`
  - Pathが一致していない、WorkflowがActiveでない
- `401/403`
  - Basic AuthまたはWebhook tokenが不一致
- `OAuth redirect_uri_mismatch`
  - Google Cloud側のRedirect URIが一致していない
- `Credential decrypt` 系エラー
  - `N8N_ENCRYPTION_KEY` を途中変更してしまった

## 11. 参考: 開発と本番の使い分け

- 開発: `http://localhost:5678`、ローカル検証優先
- 本番: `https://n8n.example.com`、安全性と継続運用を優先

同じWorkflow設計を保ちながら、資格情報とURLだけを環境別に分けると安全に運用できます。
