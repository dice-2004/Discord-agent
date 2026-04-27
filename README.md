# Discord AI Agent Bot

Discord をインターフェースにした、低リソース向けの AI エージェントです。

主な機能は次のとおりです。

- `/ask` による質問応答
- Gemini 3.1 Flash Lite を使った即答
- DuckDuckGo 検索ツール
- ChromaDB によるチャンネル分離メモリ
- 重い処理の同時実行制限とチェックポイント保存
- Research Agent へのジョブ委譲
- 内部 action のコード内実行

## ディレクトリ構成

```text
AI-agent-bot/
├── .env.example
├── docker/
│   ├── Dockerfile.main
│   └── Dockerfile.research
├── docker-compose.yml
├── requirements.txt
├── data/
│   ├── chromadb/
│   └── profiles/
│       └── initial_profile.md
├── docs/
│   ├── API.md
│   ├── DESIGN.md
│   └── change_log.md
└── src/
    ├── main_agent/
    │   ├── main.py
    │   └── core/
    ├── research_agent/
    │   └── research_agent_server.py
    └── tools/
```

## クイックスタート

1. `.env` を作成します。

```bash
cp .env.example .env
```

2. `.env` を編集し、最低限次を設定します。

- `DISCORD_TOKEN`
- `BOT_GUILD_ID`
- `ALLOWED_GUILD_IDS`
- `MAIN_AGENT_GEMINI_API_KEY`
- `RESEARCH_AGENT_GEMINI_API_KEY`

`GEMINI_API_KEY` は互換用の代替キーとして残っていますが、推奨は Main Agent と Research Agent でキーを分ける構成です。

3. コンテナを起動します。

```bash
docker compose up -d --build
```

4. ログを確認します。

```bash
docker compose logs -f main-agent
docker compose logs -f research-agent
```

## 主要な環境変数

- `MAIN_AGENT_GEMINI_API_KEY` / `RESEARCH_AGENT_GEMINI_API_KEY`
- `DISCORD_TOKEN`
- `BOT_GUILD_ID` / `ALLOWED_GUILD_IDS`
- `CHROMADB_PATH`
- `CHECKPOINT_DB_PATH`
- `MAX_CONCURRENT_HEAVY_TASKS`
- `RESEARCH_AGENT_URL` / `RESEARCH_AGENT_SHARED_TOKEN`
- `RESEARCH_AGENT_DB_PATH`
- `RESEARCH_AGENT_GEMINI_MODEL`
- `RESEARCH_AGENT_CLI_MODEL` / `RESEARCH_AGENT_CLI_FALLBACK_MODEL`
- `INTERNAL_ALLOWED_ACTIONS` / `INTERNAL_ACTION_REQUIRED_FIELDS`
- `GITHUB_TOKEN` / `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD`

詳細は [.env.example](.env.example) を参照してください。

## Research Agent

Research Agent は Main Agent からのジョブを受け取り、ジョブベースで非同期に処理します。

現行実装では、`mode` は互換用の入力として受け取り、実際の探索経路は ResearchOrchestrator 側の認証可否で決まります。

- `mode=auto` : 既定。CLI OAuth が使えるならそれを使い、使えない場合は API 側へフォールバックします。
- `mode=gemini_cli` : CLI 利用意図を示すラベルです。失敗時は API 側へフォールバックします。
- `mode=fallback` : API 側優先意図を示すラベルです。現行実装では同じ研究ループを通ります。

CLI 認証は、コンテナ内の `$HOME/.gemini/oauth_creds.json` から読み取ります。Research Agent はこれを `google-genai` で利用し、使えない場合は同じ問い合わせを Gemini API 側へフォールバックします。

## よく使うコマンド

- `/ask` : 通常の質問応答
- `/deepdive` : 調査ジョブ投入
- `/logsearch` : Discord 過去ログ検索
- `/runcli` : 承認付き CLI 実行
- `/runcli_audit` : CLI 監査ログ確認
- `/profile_show` / `/profile_set` / `/profile_forget` : ペルソナ記憶の管理
- `/auth_status` : 外部連携の認証状況確認
- `/debug_action` : 内部 action のデバッグ実行

## 認証未設定時の挙動

認証未設定の action は、`auth_required` と `auth_url` を返します。Discord に返った URL を開き、資格情報を準備してから再実行してください。

例:

- GitHub: `GITHUB_AUTH_URL`
- Google Calendar: `GOOGLE_CALENDAR_AUTH_URL`
- SMTP: `SMTP_AUTH_URL`

## 内部 action

実装済み action は次のとおりです。

- `create_github_issue`
- `backup_server_data`
- `append_sheet_row`
- `add_notion_memo`
- `add_calendar_event`
- `get_calendar_events`
- `add_task`
- `update_task`
- `delete_task`
- `bulk_update_task_due_date`
- `bulk_delete_by_dates`
- `send_email`

`add_calendar_event` と `get_calendar_events` は、必要なら `calendar_id` で参照先を上書きできます。未指定時は `GOOGLE_CALENDAR_ID` を使います。

## 実行メモ

- 永続データは `data/chromadb/` と `data/runtime/` に保存されます。
- `docs/DESIGN.md` が仕様の正本です。
- 変更履歴は [docs/change_log.md](docs/change_log.md) に追記します。

## ローカル実行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=./src
python -m main_agent.main
```
