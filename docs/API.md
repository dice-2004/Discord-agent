# Research Agent HTTP API 仕様

## 概要

Research Agent は Main Agent からの調査リクエストを受け付け、ジョブベースで非同期に処理します。

- **ベースURL**: `http://research-agent:8091`
- **認証**: `X-Research-Token` ヘッダ（共有トークン）
- **データ永続性**: SQLite (`data/runtime/research_jobs.sqlite3`)

## エンドポイント

### 1. Health Check

```http
GET /healthz
```

**レスポンス:**
```json
{
  "status": "ok",
  "service": "research-agent"
}
```

### 2. ジョブ投入

```http
POST /v1/jobs
X-Research-Token: <token>
Content-Type: application/json

{
  "topic": "調査トピック",
  "source": "auto|github|reddit|youtube|x",
  "mode": "auto|gemini_cli|fallback"
}
```

**必須フィールド:**
- `topic` (string) - 調査対象トピック

**オプション:**
- `source` (string, default: "auto") - 調査対象ソース
- `mode` (string, default: "auto") - 互換用のリクエスト指定。現行実装では保存・検証には使いますが、実際の探索経路は ResearchOrchestrator 側の認証可否で決まります。
  - `auto` - 既定。CLI OAuth が使えるならそれを使い、使えない場合は API 側へフォールバック
  - `gemini_cli` - CLI 利用を意図した指定。失敗時は API 側へフォールバック
  - `fallback` - API 優先を意図した指定。現行実装では互換ラベルとして受け取り、同じ研究ループを通る

**レスポンス (202 Accepted):**
```json
{
  "status": "queued",
  "job_id": "rj-1695000000000-1234-5678",
  "topic": "調査トピック",
  "source": "auto",
  "mode": "auto",
  "detail": "Research Agent にジョブを投入しました。"
}
```

### 3. ジョブ状態取得

```http
GET /v1/jobs/{job_id}
X-Research-Token: <token>
```

**レスポンス (200 OK):**
```json
{
  "job_id": "rj-1695000000000-1234-5678",
  "topic": "調査トピック",
  "source": "auto",
  "mode": "auto",
  "status": "done|running|queued|failed",
  "engine": "gemini_api|timeout|error",
  "report": "[Research Engine] gemini_api\nレポート本文...",
  "decision_log": [
    {
      "turn": 1,
      "action": "tool|respond",
      "tool": "web_search|read_url_markdown|source_deep_dive",
      "reason": "ツール選択理由"
    }
  ],
  "error": null,
  "created_at": "2026-04-01T14:00:00+00:00",
  "updated_at": "2026-04-01T14:05:00+00:00"
}
```

**ステータス値:**
- `queued` - 待機中
- `running` - 実行中
- `done` - 完了
- `failed` - エラー終了

**Engine 値:**
- `gemini_api` - 現行の Research Agent で完了した通常ジョブ
- `timeout` - ジョブ全体の制限超過
- `error` - 実行エラー

注記:

- 現行実装では、Research Agent の詳細な探索分岐は `decision_log` と `ai_exchange` ログで追跡します。
- `mode` は将来の厳密なルーティング用に残されていますが、現状はエンジン切替の強いスイッチではありません。

## タイムアウト

| 層 | 環境変数 | デフォルト | 説明 |
|----|---------|----------|------|
| CLI実行 | `RESEARCH_AGENT_GEMINI_TIMEOUT_SEC` | 240秒 | Gemini CLI プロセスタイムアウト |
| Orchestrator API | `RESEARCH_GEMINI_TIMEOUT_SEC` | 60秒 | 管理AI（Gemini API）呼び出しタイムアウト |
| ジョブ全体 | `RESEARCH_AGENT_JOB_TIMEOUT_SEC` | 600秒 | 投入→完了までの全体制限 |

## 詳細フロー

### Mode の実装上の扱い

現行実装では、`mode` は入力として受け取り、ジョブに記録しますが、実際の探索経路を直接分岐させる厳密なスイッチではありません。Research Agent は `ResearchOrchestrator` の中で CLI OAuth の可否を見て探索を進め、使えない場合は API キー側へフォールバックします。

- `auto` - 既定の互換指定
- `gemini_cli` - CLI 利用意図の互換指定
- `fallback` - API 優先意図の互換指定

必要なら将来、`mode` を厳密なルーティング条件に昇格できます。現状の `engine` は `gemini_api` / `timeout` / `error` のみです。

## Decision Log

`decision_log` は ResearchOrchestrator の判断ログです。CLI 可用時の探索や API フォールバックの経路がここに入ります。`engine` はジョブ全体の結果ラベルで、探索の内部経路そのものではありません。

```json
{
  "decision_log": [
    {
      "turn": 1,
      "action": "tool",
      "tool": "web_search",
      "reason": "初期情報取得のため web_search を実行"
    },
    {
      "turn": 2,
      "action": "tool",
      "tool": "read_url_markdown",
      "reason": "詳細確認のため重要 URL の本体を読み込み"
    },
    {
      "turn": 2,
      "action": "respond",
      "reason": "十分な情報が得られたため回答"
    }
  ]
}
```

## エラーハンドリング

### 400 Bad Request

```json
{
  "status": "error",
  "code": "invalid_topic",
  "detail": "topic が空です。"
}
```

**Codes:**
- `invalid_topic` - topic が未指定/空
- `invalid_json` - リクエスト JSON が不正
- `invalid_job_id` - job_id が不正な形式

### 403 Forbidden

```json
{
  "status": "error",
  "code": "forbidden",
  "detail": "認証トークンが不正です"
}
```

### 404 Not Found

```json
{
  "status": "error",
  "code": "job_not_found",
  "job_id": "rj-..."
}
```

### 500 Internal Server Error

```json
{
  "status": "error",
  "code": "store_unavailable",
  "detail": "データベースに接続できません"
}
```

## 使用例

### Python requests

```python
import requests
import time

base_url = "http://research-agent:8091"
token = "change_me"

headers = {
    "X-Research-Token": token,
    "Content-Type": "application/json"
}

# 1. ジョブ投入
response = requests.post(
    f"{base_url}/v1/jobs",
    headers=headers,
    json={
        "topic": "Claude ソースコード流出",
        "source": "auto",
        "mode": "auto"
    }
)
assert response.status_code == 202
job_id = response.json()["job_id"]
print(f"Job submitted: {job_id}")

# 2. ジョブ状態をポーリング
while True:
    response = requests.get(
        f"{base_url}/v1/jobs/{job_id}",
        headers=headers
    )
    job = response.json()
    status = job["status"]
    print(f"Status: {status}")

    if status in {"done", "failed"}:
        break

    time.sleep(3)

# 3. 結果確認
print(f"Engine: {job['engine']}")
print(f"Report: {job['report'][:500]}")
if job.get("decision_log"):
    for log in job["decision_log"]:
        print(f"  Turn {log['turn']}: {log['action']} - {log.get('tool', '')} ({log['reason']})")
```

### curl

```bash
TOKEN="change_me"

# ジョブ投入
JOB_ID=$(curl -sS -X POST http://research-agent:8091/v1/jobs \
  -H "X-Research-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"topic":"test","source":"auto","mode":"auto"}' \
  | jq -r '.job_id')

echo "Job ID: $JOB_ID"

# ジョブ状態取得
curl -sS -X GET "http://research-agent:8091/v1/jobs/$JOB_ID" \
  -H "X-Research-Token: $TOKEN" | jq .
```

## 版履歴

- **v1** (2026-04-01): 初版
  - 2層構成（CLI + Orchestrator）
  - decision_log フィールド追加
  - タイムアウト層明示化
