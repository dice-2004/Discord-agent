# Discord AI Agent Bot

Discordをインターフェースにした、低リソース向けのAIエージェントです。
現在の標準構成では以下を提供します。

- `/ask` での質問応答
- Gemini 3.1 Flash Lite を使った回答生成
- DuckDuckGo検索ツール（必要時のみ利用）
- ChromaDBによるチャンネル分離メモリ
- 重い処理の同時実行制限（キュー制御）
- SQLiteによる長時間タスクのチェックポイント基盤
- Dockerコンテナでの実運用前提

## ディレクトリ構成

```text
AI-agent-bot/
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── data/
│   ├── chromadb/
│   └── profiles/
│       └── initial_profile.md
├── docs/
│   └── DESIGN.md
└── src/
    └── discord_ai_agent/
        ├── main.py
        ├── core/
        └── tools/
```

## クイックスタート（Docker）

1. `.env` を作成

```bash
cp .env.example .env
```

2. `.env` を編集し、最低限次を設定

- `DISCORD_TOKEN`
- `GEMINI_API_KEY`
- `BOT_GUILD_ID`
- `ALLOWED_GUILD_IDS`

3. コンテナをビルド・起動

```bash
docker compose build
docker compose up -d
```

4. ログ確認

```bash
docker compose logs -f discord-ai-agent
```

## 外部アクション実行（コード内）

このプロジェクトは n8n 中継を使わず、Botコード内で action を直接実行します。

### 1. 主要な環境変数

- `INTERNAL_ALLOWED_ACTIONS`
- `INTERNAL_ACTION_REQUIRED_FIELDS`
- `INTERNAL_ACTION_TIMEOUT_SEC`
- `CALENDAR_PROVIDER` / `GOOGLE_CALENDAR_ID` / `GOOGLE_CALENDAR_CLIENT_ID` / `GOOGLE_CALENDAR_CLIENT_SECRET` / `GOOGLE_CALENDAR_REFRESH_TOKEN`
- `GITHUB_TOKEN` / `GITHUB_AUTH_URL`
- `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` / `SMTP_AUTH_URL`（メール送信を使う場合のみ）
- `BACKUP_OUTPUT_DIR` / `BACKUP_ALLOWED_ROOTS`
- `SHEET_STORAGE_DIR`
- `NOTION_MEMO_STORAGE_PATH`
- `CALENDAR_EVENTS_STORAGE_PATH` / `CALENDAR_EVENTS_LIST_LIMIT`

### 2. Discord からの実行

- 通常運用は `/ask` を使います（AIが必要なツールを自律的に選択して実行）
- 認証状態確認: `/auth_status`
- デバッグ用手動実行: `/debug_action action:<name> payload_json:<json>`

### 3. 認証未設定時の運用

認証未設定で実行すると、`auth_required` と `auth_url` を返します。Discordに返ったURLを開き、資格情報を準備してから再実行してください。

例:

- GitHub: `GITHUB_AUTH_URL`（既定: `https://github.com/settings/tokens`）
- Google Calendar: `GOOGLE_CALENDAR_AUTH_URL`（OAuth クライアント + refresh token の作成先）
- SMTP: `SMTP_AUTH_URL`（運用サービスの設定ページURLを指定）

### 4. 注意点

- 実装済み action: `create_github_issue`, `backup_server_data`, `append_sheet_row`, `add_notion_memo`, `add_calendar_event`, `get_calendar_events`（`send_email` は任意）
- `add_calendar_event` / `get_calendar_events` は payload に `calendar_id` を指定すると参照先カレンダーを上書きできます（未指定時は `GOOGLE_CALENDAR_ID`）。
- `stub-success` のような疑似成功は返しません。

## Discord Botセットアップ手順（初学者向け）

1. Discord Developer Portalにアクセス

- https://discord.com/developers/applications

2. New Applicationを作成

- 任意の名前でアプリを作成

3. Botを作成

- 左メニュー `Bot` -> `Add Bot`
- `Reset Token` または `Copy` でトークンを取得
- この値を `.env` の `DISCORD_TOKEN` に設定

4. Privileged Gateway Intents

- 現行構成はスラッシュコマンド中心なので、基本はデフォルトで可
- メンション応答などを拡張する際は必要に応じて有効化

5. Botをサーバーへ招待

- 左メニュー `OAuth2` -> `URL Generator`
- `SCOPES` で `bot` と `applications.commands` を選択
- `BOT PERMISSIONS` は最低限以下を付与
  - `Send Messages`
  - `Attach Files`
  - `Read Message History`
- 生成URLを開き、対象サーバーへ追加

6. サーバーIDを取得

- Discordの開発者モードをON
- 対象サーバーを右クリックしてIDをコピー
- `.env` の `BOT_GUILD_ID` / `ALLOWED_GUILD_IDS` に設定

7. `/ask` コマンド確認

- Bot起動後、サーバーで `/ask` を実行
- 応答が返ればセットアップ完了

8. `@メンション` での質問（任意）

- `.env` で `DISCORD_ENABLE_MESSAGE_CONTENT_INTENT=true` と `MENTION_ASK_ENABLED=true` を有効化
- `@agent-bot 今日の予定を教えて` のようにメンション先頭で送ると、`/ask` 相当として処理されます
- `MENTION_REQUIRE_PREFIX=true` の場合、文中メンションでは発火せず先頭メンションのみ反応します
- `MENTION_QUICK_CALENDAR_ENABLED=true` の場合、カレンダー系の定型依頼はLLMを経由せず直接 action 実行します（高速・安定化）

## 運用メモ

- 永続データは `data/chromadb/` に保存されます
- `data/chromadb/` と `.env` は `.gitignore` 対象です
- 仕様変更時は `docs/DESIGN.md` を最優先で更新してください
- 変更履歴は `docs/change_log.md` に追記して管理してください

## 開発時のローカル実行（任意）

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=./src
python -m discord_ai_agent.main
```
