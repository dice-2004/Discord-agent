# Discord AI Agent Bot (Phase 1)

Discordをインターフェースにした、低リソース向けのAIエージェントです。
Phase 1では以下を提供します。

- `/ask` での質問応答
- Gemini 3.1 Flash Lite を使った回答生成
- DuckDuckGo検索ツール（必要時のみ利用）
- ChromaDBによるチャンネル分離メモリ
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

## n8nセットアップ（Docker / セルフホスト）

このプロジェクトでは、n8nを同じ `docker compose` で起動し、Botは内部ネットワーク経由でWebhookを呼び出します。

本番運用を含む詳細手順は次を参照してください。

- `docs/N8N_DEPLOYMENT_BEGINNER_GUIDE.md`（初心者向け。クリック手順付き）

### 1. n8n用の .env を設定

最低限、以下を `.env` で設定してください。

- `N8N_BASIC_AUTH_USER`
- `N8N_BASIC_AUTH_PASSWORD`
- `N8N_ENCRYPTION_KEY`（32文字以上推奨）
- `N8N_WEBHOOK_TOKEN`

補足:

- Bot→n8n 呼び出し先は `N8N_WEBHOOK_BASE_URL=http://n8n:5678/webhook/execute_n8n_workflow`（コンテナ間通信）
- ブラウザからの管理UIは `http://localhost:5678`

### 2. n8nを起動

```bash
docker compose up -d n8n
docker compose logs -f n8n
```

### 3. n8n Web UI にログイン

1. ブラウザで `http://localhost:5678` を開く
2. Basic Auth のユーザー/パスワードを入力（`.env` の `N8N_BASIC_AUTH_USER/PASSWORD`）

### 4. `execute_n8n_workflow` 受け口ワークフローを作成

GUIを使わずCUIで作成する場合（推奨）:

```bash
docker compose cp n8n/workflows/execute_n8n_workflow.json n8n:/tmp/execute_n8n_workflow.json
docker compose exec -T n8n n8n import:workflow --input=/tmp/execute_n8n_workflow.json
docker compose exec -T n8n n8n publish:workflow --id=CQ6P0owt0ZMIzZP6
docker compose restart n8n
```

登録確認:

```bash
docker compose exec -T n8n n8n export:workflow --id=CQ6P0owt0ZMIzZP6
```

`"active": true` を確認してください。

推奨構成:

1. `Webhook` ノードを追加
2. Path を `execute_n8n_workflow` に設定
3. Method は `POST`
4. `IF` / `Switch` ノードで `action` を分岐
5. 各分岐で Google Calendar / Email ノードへ接続
6. 最後に `Respond to Webhook` で JSONを返す

期待する受信JSON:

```json
{
  "action": "add_calendar_event",
  "parameters": {
    "title": "ゼミ",
    "start_time": "2026-04-10T15:00:00+09:00",
    "end_time": "2026-04-10T16:00:00+09:00"
  }
}
```

### 5. Webhookトークン検証（推奨）

Botは `X-Webhook-Token` ヘッダを付与します（`.env` の `N8N_WEBHOOK_TOKEN`）。

1. `Webhook` の直後に `IF` ノードを追加
2. ヘッダ `x-webhook-token` が期待値と一致するか判定
3. 不一致なら `Respond to Webhook` で 403 を返す

### 6. Google Calendar連携（n8n内 OAuth）

#### Google Cloud 側

1. Google Cloud Consoleでプロジェクトを作成
2. `Google Calendar API` を有効化
3. OAuth 同意画面を設定（External でも可）
4. OAuth クライアントIDを作成（Web application）
5. Authorized redirect URI に以下を追加

`http://localhost:5678/rest/oauth2-credential/callback`

#### n8n 側

1. Google Calendar ノードで新規Credential作成
2. Client ID / Client Secret を入力
3. `Connect my account` でGoogle認可
4. 対象Googleアカウントへのアクセスを許可

### 7. Bot連携の最終確認

1. n8nワークフローを `Active` にする
2. `.env` の `N8N_ALLOWED_ACTIONS` と `N8N_ACTION_REQUIRED_FIELDS` をワークフローに合わせる
3. Botから `ask` で予定追加/参照要求を出し、n8nログと実データを確認

### 8. メール送信（将来）

SMTP利用時:

- SMTP host / port / user / password

API型利用時（SendGrid等）:

- APIキー

### 9. セキュリティ推奨

- `N8N_BASIC_AUTH_PASSWORD` を強力な値へ変更
- `N8N_ENCRYPTION_KEY` を32文字以上のランダム文字列へ変更
- `N8N_WEBHOOK_TOKEN` を十分長いランダム値へ変更
- 公開時はリバースプロキシでIP制限または認証を追加
- 開発中も `127.0.0.1:5678` バインドを維持（外部公開しない）

## 本番環境について（要点）

結論として、**本番でも同様の設定思想は必要**です。
ただし、以下は必ず本番専用値に置き換えてください。

- `N8N_BASIC_AUTH_USER` / `N8N_BASIC_AUTH_PASSWORD`（強固な本番用資格情報）
- `N8N_ENCRYPTION_KEY`（本番専用の固定値。再生成で既存Credentialが読めなくなる可能性あり）
- `N8N_WEBHOOK_TOKEN`（十分長いランダム値）
- `WEBHOOK_URL`（本番FQDN + HTTPS）

本番の具体的な手順（DNS/TLS/リバースプロキシ/バックアップ/復旧）は以下を参照。

- `docs/N8N_DEPLOYMENT_BEGINNER_GUIDE.md` の「本番環境への移行手順」

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

- Phase 1はスラッシュコマンド中心なので、基本はデフォルトで可
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
