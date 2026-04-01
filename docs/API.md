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
- `mode` (string, default: "auto")
  - `auto` - CLI が使える場合優先、不可なら fallback
  - `gemini_cli` - Gemini CLI のみ使用
  - `fallback` - Gemini CLI を使わず管理AI（Gemini API）を優先。失敗時のみ deep_dive

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
  "engine": "gemini_cli|orchestrator|deep_dive|gemini_cli+orchestrator|timeout|error",
  "report": "[Research Engine] gemini_cli\nレポート本文...",
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
- `gemini_cli` - Gemini CLI のみで完了
- `orchestrator` - 管理AI（Gemini API）主導で完了
- `deep_dive` - Web 検索/Deep Dive で完了
- `gemini_cli+orchestrator` - CLI 初期探索 + 管理AI（Gemini API）ツール活用で完了
- `timeout` - タイムアウト
- `error` - 実行エラー

## タイムアウト

| 層 | 環境変数 | デフォルト | 説明 |
|----|---------|----------|------|
| CLI実行 | `RESEARCH_AGENT_GEMINI_TIMEOUT_SEC` | 240秒 | Gemini CLI プロセスタイムアウト |
| Orchestrator API | `RESEARCH_GEMINI_TIMEOUT_SEC` | 60秒 | 管理AI（Gemini API）呼び出しタイムアウト |
| ジョブ全体 | `RESEARCH_AGENT_JOB_TIMEOUT_SEC` | 600秒 | 投入→完了までの全体制限 |

## 詳細フロー

### Mode: auto（推奨）

```
1. Gemini CLI が有効か？
   ├─ YES: CLI を実行
   │   ├─ 成功 → レポート返却
   │   └─ 失敗 → 次へ
   └─ NO: 2へ

2. "need_orchestrator" マーカーがあるか？
   ├─ YES: 管理AI（Gemini API）+ ツール起動
   │   └─ レポート返却
   └─ NO: Deep Dive で補完
       └─ レポート返却
```

### Mode: gemini_cli

- Gemini CLI のみ使用
- CLI が利用不可/失敗の場合は error を返す

### Mode: fallback

- CLI を使わず、管理AI（Gemini API）を優先
- 管理AIが利用不可/失敗時のみ Deep Dive へフォールバック

## Decision Log

`decision_log` は **管理AI（Gemini API）が起動された場合のみ**含まれます。
CLI のみで完了した場合は空配列 `[]` です。

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
