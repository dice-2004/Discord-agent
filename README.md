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
