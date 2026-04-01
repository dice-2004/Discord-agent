# AI Agent Bot - 詳細アーキテクチャ

## システム全体像

```
┌─────────────────────────────────────────────────────────────────┐
│ Discord Server                                                   │
│  ├─ User Messages (@mention, /command)                          │
│  └─ Rich Embed Responses                                         │
└────────────────────────┬────────────────────────────────────────┘
                         │
                    HTTP API
                         │
        ┌────────────────┴────────────────┐
        │                                 │
┌──────▼──────────────────┐    ┌────────▼────────────────┐
│ Main Agent               │    │ Research Agent          │
│ (FastAPI + discord.py)   │    │ (FastAPI + subprocess)  │
│                          │    │                        │
│ ├─ /research endpoint    │    │ ├─ /v1/jobs POST       │
│ ├─ Tool selection logic  │    │ ├─ /v1/jobs GET        │
│ ├─ chroma DB (memory)    │    │ ├─ /healthz            │
│ ├─ Discord Gateway       │    │ └─ Multi-turn handling  │
│ └─ Command handlers      │    │                        │
│    /deepdive             │    │ Subprocess (Gemini CLI)│
│    /web_search           │    │ + Gemini API           │
│    /ask_research         │    │ (Orchestrator)         │
└──────────────────────────┘    └────────────────────────┘
        │                                │
        └────────────────┬───────────────┘
                         │
            ┌────────────┴─────────────┐
            │                          │
        ┌───▼────┐            ┌───────▼──┐
        │ Gemini │            │ Web APIs │
        │  API   │            │          │
        │        │            ├─ Reddit  │
        │ ├─ CLI │            ├─ GitHub  │
        │ └─ REST│            ├─ YouTube │
        └────────┘            └──────────┘
```

## コンポーネント詳細

### 1. Main Agent (`src/main_agent/`)

**職責:**
- Discord ゲートウェイ接続
- ユーザーコマンド・メッセージ処理
- Research Agent への委譲判定
- 記憶管理（ChromaDB）
- 結果を Discord に返却

**主要ファイル:**
- `main_agent_server.py` - FastAPI サーバー + discord.py client
- `discord_handlers.py` - メッセージハンドラ / コマンドハンドラ
- `memory/` - ChromaDB インテグレーション

**フロー:**

```
User @mention → Discord Gateway
              ↓
        Message Handler
              ↓
      Analysis (intent)
              ↓
   Create Research Job?
     /  \
   YES  NO
    /     \
Research   Direct
  Job    Response
   \       /
    ↓     ↓
  Return to Discord
```

### 2. Research Agent (`src/research_agent/`)

**職責:**
- 非同期ジョブキュー管理
- Gemini CLI 実行 + 結果分析
- need_orchestrator 判定
- Orchestrator（Gemini API）ワークフロー実行
- ジョブ永続化（SQLite）

**スタック:**
- FastAPI (HTTP API)
- SQLite (ジョブ永続化)
- subprocess (Gemini CLI)
- asyncio (ジョブワーカー)
- Gemini SDK（API）

**サブコンポーネント:**

#### 2.1 Research Orchestrator (`core/orchestrator.py`)

Gemini API ベースの最小限管理エージェント。CLI の結果が "需要", "必要", "深掘り" 等のマーカーを含む場合に起動。

**特徴:**
- 最大 2ターン（往信 #1 → ツール選択 #2 → 最終回答）
- 自動ツール選択ロジック
- decision_log 記録

**内部クラス:**

```python
class ResearchOrchestrator:
    def __init__(self, model_name, tools_registry):
        self.client = genai.Client()
        self.model = model_name  # "gemini-2.0-flash" 等
        self.tools_registry = tools_registry

    async def answer(self, prompt, max_turns=2):
        """
        初期クエリに対し、最大 max_turns で回答
        -> {answer, decision_log}
        """
        # Turn 1: 初期分析 + ツール選択
        # Turn 2: ツール結果に基づく最終回答
```

**ツール呼び出しのモデル:**
```
Gemini API (Claude の role="user")
   ↓
   "web_search で X を検索してください"
   ↓
Tool Executor (ResearchToolRegistry)
   ↓
   "検索結果: ..."
   ↓
Gemini API (Claude の role="assistant" + tool result)
   ↓
   "... 以下のように判明しました"
```

#### 2.2 Tool Registry (`tools/registry.py`)

Research Agent が使用可能なツールを登録・実行。

**登録ツール:**
1. `web_search(query, num_results=5)`
   - DuckDuckGo / Google 検索
   - 最新情報取得

2. `read_url_markdown(url)`
   - URL 本体を Markdown 変換
   - 記事内容取得

3. `source_deep_dive(topic, source="auto")`
   - Reddit/GitHub/YouTube/X 深掘り
   - プラットフォーム固有情報

**Tool Schema（Gemini 向け）:**
```json
{
  "name": "web_search",
  "description": "Search the web for information",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "Search query"
      }
    },
    "required": ["query"]
  }
}
```

#### 2.3 Job Store (`core/job_store.py`)

SQLite ベースの永続化層。

**スキーマ:**
```sql
CREATE TABLE research_jobs (
  job_id          TEXT PRIMARY KEY,
  topic           TEXT NOT NULL,
  source          TEXT DEFAULT 'auto',
  mode            TEXT DEFAULT 'auto',
  status          TEXT DEFAULT 'queued',
  engine          TEXT,
  report          TEXT,
  decision_log    TEXT,  -- JSON array
  error           TEXT,
  created_at      TIMESTAMP,
  updated_at      TIMESTAMP
)
```

**主要メソッド:**
- `create_job(topic, source, mode)` → job_id
- `update_job(job_id, status, **kwargs)`
- `get_job(job_id)` → dict
- `list_jobs_by_status(status)` → list

#### 2.4 Research Server (`research_agent_server.py`)

HTTP API サーバー。ジョブワーカーを管理。

**エンドポイント:**
- `POST /v1/jobs` - ジョブ投入
- `GET /v1/jobs/{job_id}` - ステータス取得
- `GET /healthz` - ヘルスチェック

**ジョブワーカー (`_worker()`):**

```
┌──────────────────────────────────────┐
│ Job Worker Thread                     │
│                                      │
│ for job_id in queue:                 │
│   start_time = time.time()           │
│                                      │
│   while elapsed < JOB_TIMEOUT:       │
│     if status in {done, failed}:     │
│       break                          │
│     if CLI_AVAILABLE:                │
│       → _run_gemini_cli()            │
│       → _check_need_orchestrator()   │
│       → YES? _run_orchestrator...()  │
│     else:                            │
│       → _run_orchestrator_deepdive() │
│                                      │
│     update_job(status, report, ...)  │
│                                      │
└──────────────────────────────────────┘
```

## データフロー詳細

### シナリオ 1: Gemini CLI のみで完了

```
User Request
  ↓
Research Agent /v1/jobs
  ↓
Job Queue (queued)
  ↓
Worker: _run_gemini_cli()
  ├─ subprocess.run("gcloud ai language  ...")
  └─ stdout = CLI結果
  ↓
_check_need_orchestrator(stdout)
  └─ "need_orchestrator" マーカー検出？ → NO
  ↓
Job Status = done
Engine = "gemini_cli"
Report = stdout
decision_log = []
```

### シナリオ 2: CLI + Orchestrator

```
User Request
  ↓
Research Agent /v1/jobs
  ↓
Job Queue (queued)
  ↓
Worker: _run_gemini_cli()
  ├─ subprocess.run("gcloud ai language ...")
  └─ stdout = CLI結果
  ↓
_check_need_orchestrator(stdout)
  └─ "need_orchestrator" マーカー検出？ → YES
  ↓
_run_orchestrator_deepdive(topic, stdout)
  ├─ ResearchOrchestrator.answer(prompt)
  │  ├─ Turn 1: Gemini API に「以下の情報をもとに追加調査の必要性を判定」
  │  │           + web_search / read_url / source_deep_dive の候補
  │  │
  │  ├─ ツール実行 (ResearchToolRegistry)
  │  │  ├─ tool_result = await registry.execute(tool_name, params)
  │  │  └─ decision_log.append({turn, action, tool, reason})
  │  │
  │  └─ Turn 2: Gemini API に「ツール結果を踏まえた最終回答」
  │
  └─ final_report = orchestrator_response
  ↓
Job Status = done
Engine = "gemini_cli+orchestrator"
Report = final_report
decision_log = [{turn: 1, action: "tool", tool: "web_search", reason: "..."}, ...]
```

### シナリオ 3: Fallback (Orchestrator 優先)

```
User Request (RESEARCH_AGENT_USE_GEMINI_CLI=false)
  ↓
Worker: Check mode="fallback"
  └─ CLI をスキップ
  ↓
_run_orchestrator_deepdive(topic)
  ├─ ResearchOrchestrator に「topic についてレポートを作成」
  ├─ Orchestrator が ツール選択・実行
  └─ decision_log 記録
  ↓
Job Status = done
Engine = "orchestrator"
Report = orchestrator_response
decision_log = [{...}, ...]

(管理AIが未設定/失敗時のみ deep_dive へフォールバック)
```

## 環境設定・タイムアウト

### 環境変数

```bash
# Gemini CLI
RESEARCH_AGENT_USE_GEMINI_CLI=true
RESEARCH_AGENT_GEMINI_TIMEOUT_SEC=240

# Orchest API
RESEARCH_GEMINI_TIMEOUT_SEC=60

# Job 全体
RESEARCH_AGENT_JOB_TIMEOUT_SEC=600

# API 認証
RESEARCH_AGENT_TOKEN=change_me
```

### タイムアウト構造

| 層 | 用途 | TimeoutSec | トリガーポイント |
|----|------|-----------|---------------|
| **Stage 1** | Gemini CLI 実行 | 240 | `subprocess.run(..., timeout=240)` |
| **Stage 2** | Orchestrator (Gemini API) | 60 | `asyncio.wait_for(..., timeout=60)` |
| **Stage 3** | ジョブ全体 | 600 | `_worker()` の `elapsed > timeout` チェック |

**多重タイムアウトの意味:**
- CLI が 200秒で終了 → Stage 1 OK
- Orchestrator が 50秒で終了 → Stage 2 OK
- ジョブ全体が 550秒 → Stage 3 OK (600秒制限内)
- ↑ ただし、いずれか 1 つが超過すると即座にエラー

## 永続化戦略

### SQLite (`data/runtime/research_jobs.sqlite3`)

**用途:**
- ジョブ履歴の保存
- Status tracking (queued → running → done/failed)
- Report / decision_log の永続化

**一貫性:**
- UPDATE 操作は単一スレッド（Worker）
- SELECT 操作は複数スレッド可（GET /v1/jobs/{id}）
- WAL モード有効

**Cleanup:**
- 7日以上前の completed/failed ジョブは定期削除（未実装）

## セキュリティ

### 認証

- **ヘッダ**: `X-Research-Token`
- **検証**: `if token != os.getenv("RESEARCH_AGENT_TOKEN"): raise 403`
- **デフォルト**: "change_me" (本番環境では変更必須)

### 入力検証

- `topic` 長さ: 最大 1000 文字
- `source` / `mode` は enum チェック
- JSON パース エラー時は 400 Bad Request

### 出力サニタイズ

- Report / decision_log は JSON エスケープ
- Gemini API からの応答は型チェック

## スケーリング

### 現在の制約

- **SQLite ロック**: 複数 Worker は未対応（単一ワーカー前提）
- **メモリ**: Report サイズ >= 100KB の場合は disk streaming 考慮
- **ネットワーク**: 研究エージェント API の呼び出しは順序実行

### 将来の拡張

1. **Worker スケール**: PostgreSQL + 複数ワーカー
2. **キャッシング**: Redis for search results
3. **モニタリング**: Prometheus + Grafana
4. **ロギング**: ELK stack

## デバッグ

### ログレベル設定

```python
# research_agent_server.py
import logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
```

### Job の確認

```bash
# SQLite クエリ
sqlite3 data/runtime/research_jobs.sqlite3 "SELECT job_id, status, engine, created_at FROM research_jobs ORDER BY created_at DESC LIMIT 5;"

# HTTP で確認
curl http://research-agent:8091/v1/jobs/rj-1695000000000-xxxx -H "X-Research-Token: change_me" | jq .
```

### CLI 出力の確認

```bash
# 一時的に stdout リダイレクト
docker logs research-agent 2>&1 | grep -i "gemini_cli"
```

## 版履歴

- **v2** (2026-04-01)
  - 2層構造詳細化
  - タイムアウト層明示化
  - Orchestrator ワークフロー説明
- **v1** (2026-03-15)
  - 初版（Main + Research 分離）
