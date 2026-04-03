# 不具合トラッカー

本ファイルはテスト実施過程で検出された不具合を統合管理します。各不具合について、根本原因、再現手順、改善提案を記録し、修正進捗を追跡します。

## 概要

| ステータス | 件数 | 優先度内訳 |
|---|---|---|
| **Open** | 0 | Critical: 0 / High: 0 / Medium: 0 |
| **In Progress** | 0 | — |
| **Fixed** | 13 | Critical: 3 / High: 6 / Medium: 4 |
| **Pending Verification** | 1 | Medium: 1 |

---

## Critical 優先度（システム動作に影響）

### BUG-001: Deep-dive Force Dispatch 判定ロジック不完全

- **ステータス**: Fixed
- **優先度**: Critical
- **テスト ID**: MT-014, MT-016, MT-033
- **検出日時**: 2026-04-03 00:30
- **根本原因**: `orchestrator._should_force_research_job()` が比較・対策・実例系クエリを検出できない
  - 現在の判定ロジック: キーワード抽出が「調べて」「詳しく」など限定的
  - 問題: 「Gemini APIのthought_signature対策を**比較**して」のような"対比"表現が未対応
  - 影響: Research Agent の自動委譲が発動せず、内部AI応答だけで不十分な回答になる

**再現手順**:
```
1. /ask "Gemini APIのthought_signatureエラー対策を比較して" を実行
   期待: _should_force_research_job() = true → dispatch_research_job() へ移行
   実際: _should_force_research_job() = false → 内部Gemini AI応答のみ
```

**改善提案**:
- 判定ロジックに以下キーワードパターンを追加:
  - "対比/比較/違い": 複数オプションの並列判定が必要
  - "対策/解決/手段": 具体的なソリューション検索が必要
  - "前例/事例/実例": リアルワールド情報が必要
  - "国内/海外/事例サイト": 地理・出典別検索が必要
- コード修正箇所: `src/main_agent/core/orchestrator.py` lines ~400-420

---

### BUG-002: Follow-up コンテキスト ドリフト

- **ステータス**: Fixed
- **優先度**: Critical
- **テスト ID**: CT-001, UT-004, UT-005, MT-017, MT-021, QLP-007, QLP-008
- **検出日時**: 2026-04-02 22:44
- **根本原因**: Follow-up marker 検出後のコンテキスト注入が直前トピックを参照せず、別の近傍会話を参照している
  - 問題パターン1: 「さっきの内容をもう一度」→ 最新の asyncio 要約へ遷移（CT-001/UT-004）
  - 問題パターン2: 「その3つの違いは？」→ Python 3項目ではなく async/await へ逸脱（UT-005）
  - 問題パターン3: 多段フォローアップで異なる話題へ段階的に逸脱（MT-021）
  - 影響: 指示語「その/それ/あの」の参照先が意図と異なる回答生成

**再現手順**:
```
Q1: /ask "推奨フレームワーク3つ" → [Flask/Django/FastAPI など回答]
Q2: /ask "その3つの違いは？"
   期待: Q1 の[Flask/Django/FastAPI] 3項目の比較を返回
   実際: 別の直前文脈（asyncio 3点など）へ遷移して回答
```

**改善提案**:
- 機能: `_extract_followup_topic_from_recent_context()` および `_build_follow_up_prompt()` の改善
- 対象: Recent conversation window の構築ロジックを見直し
  - 現状: `recent_limit=40` で直近40トークンの会話を参照
  - 問題: 40トークンでは Q1 全体がカットされる場合がある
  - 提案: 最後の Q1-A1 のペアを確実に含めるまで拡張（`recent_limit=100`）
- コード修正箇所: `src/main_agent/core/orchestrator.py` lines ~600-650
- テスト強化: Follow-up marker 検出のデバッグログ出力を追加

---

### BUG-003: Calendar 統合エラー（Get 経路）

- **ステータス**: Fixed
- **優先度**: Critical
- **テスト ID**: CT-010
- **検出日時**: 2026-04-03 00:34
- **根本原因**: `execute_internal_action(add_calendar_event, ...)` は成功（`status: ok`）したが、その直後の `get_calendar_events` がエラー返却
  - `add_calendar_event`: `{"status": "ok", "event_id": "cal-..."}` ✓
  - `get_calendar_events`: `{"code": "error", ...}` ✗
  - 影響: 予定データが Prompt 文脈へ注入されず、「今週の予定は？」への回答が一般的な情報のみ

**再現手順**:
```
1. execute_internal_action("add_calendar_event", {"title":"会議","start_time":"2026-04-03T10:00:00","end_time":"2026-04-03T11:00:00"})
   → status: ok ✓
2. execute_internal_action("get_calendar_events", {"time_min":"2026-04-01T00:00:00","time_max":"2026-04-30T23:59:59"})
   → error ✗
```

**改善提案**:
- 根本調査: Calendar storage 実装確認
  - ファイルパス: `/app/data/runtime/calendar_events.jsonl` の存否と書き込み権限確認
  - Get 処理: `_handle_get_calendar_events()` でのファイル読み込みロジック確認
  - 可能性: Add は RAM キャッシュ、Get が永続化ストレージを参照している可能性
- 修正手段:
  1. Add/Get の保存・参照先を統一（推奨: 永続化ストレージ）
  2. または Get 処理で RAM キャッシュも参照対象に含める
- コード修正箇所: `src/tools/action_tools.py` lines ~600-700
- テスト: `_handle_add_calendar_event` / `_handle_get_calendar_events` の連続実行テスト

---

## High 優先度（機能が部分的に動作しない）

### BUG-004: Discord Callback 通知経路が Test Harness で観測不可

- **ステータス**: Pending Verification（CT-006 待ち）
- **優先度**: High
- **テスト ID**: CT-004, CT-006
- **検出日時**: 2026-04-03 00:33
- **根本原因**: Discord callback 依存の非同期通知が、テスト harness では Discord Bot 実行コンテキストなしで観測不可
  - 現象: `dispatch_research_job(wait=false)` は正しく queued ジョブを返し、`get_research_job_status` は完了を返す
  - しかし最終メッセージの Discord channel への通知は harness では観測できない
  - 影響: 自動テストでは「通知確認」が不可能

**再現手順**:
```
1. dispatch_research_job(topic="kubernetes", wait=false, mode="auto")
   → {"status": "queued", "job_id": "job-123"} ✓
2. [ジョブ完了待機]
3. get_research_job_status(job_id="job-123")
   → {"status": "done", "result": "..."} ✓
4. [Discord 同一チャンネルへの自動通知メッセージ投稿]
   → Test harness では観測不可 (Discord Bot callback path)
```

**改善提案**:
- 根本的には Discord 上での手動確認が必要（テスト harness 限界）
- 代替手段:
  1. Test harness に `asyncio.run()` で Discord coro を実行する方式へ修正
  2. または `MANUAL_TEST_ITEMS.md` にて CT-004/006 を手動テスト対象へ再分類
- ステータス: **Manual Test (Discord 実環境) へ移行を推奨**

**実機検証状況（2026-04-03 更新）**:
- CT-004 相当の `/deepdive` では、`research-notify status=done` と結果通知ログを実機で観測済み。
- CT-006（wait=true の polling 表示確認）は手順未確定で後回し。

**実機クローズ条件（2026-04-03 更新）**:
- CT-004/CT-006 の両方で、`queued(job_id)` 確認後に**同一チャンネルへ完了通知**が投稿されること。
- 上記観測を `docs/TEST_RESULTS.md` に反映し、CT-004/CT-006 を PASS 化できること。
- 実施手順は `docs/MANUAL_TEST_ITEMS.md` の「BUG-004 / BUG-010 実機検証ランブック」を参照。

---

### BUG-005: format 指定「その」の曖昧性検出失敗

- **ステータス**: Fixed
- **優先度**: High
- **テスト ID**: CT-026, QLP-012
- **検出日時**: 2026-04-02 23:55
- **根本原因**: 指示語「その」を含むクエリが、直前質問ではなく、より遠い別の Topic へ引っ張られる
  - 例: `/ask "「その料理は何ですか」という質問で"`
  - 期待: クエリ自体が言語学的な質問（例・引用部位）
  - 実際: 直前 Topic「GitHub について」へ context 注入が発生し、GitHub 関連の説明を返す

**再現手順**:
```
1. Q1: /ask "GitHub について教えて" → GitHub 概要回答
2. Q2: /ask "「その料理は何ですか」という質問で"
   期待: 言語学的な質問分析（「その」の使い方など）
   実際: GitHub context が注入され、GitHub 関連話題へ寄る
```

**改善提案**:
- 機能: 指示語判定ロジックの精密化
  - 現状: 「その/それ/あの」を全て follow-up marker として検出
  - 問題: 文内に引用符「「」」で囲まれた部分がある場合、引用内容への参照として扱うべき
  - 提案: 引用部分を remove したテキストで follow-up 判定を実施
- コード修正箇所: `src/main_agent/core/orchestrator.py` lines ~550-580

---

### BUG-006: Recent Context 想起精度不足

- **ステータス**: Fixed
- **優先度**: High
- **テスト ID**: QLP-007, QLP-008
- **検出日時**: 2026-04-02 20:37
- **根本原因**: 想起質問「さっき話した内容は？」が直近トピックではなく過去の異なる話題（Claude Code系など）を返す
  - 原因推定: MEMORY_RETRIEVAL_SCOPE の設定または semantic search の類似度スコア判定が不十分

**再現手順**:
```
1. Q1: "Python async/awaitを3点で説明して" → [3点説明]
2. Q2: "さっき話した内容は？"（想起質問）
   期待: Q1 の async/await 3点を想起
   実際: 過去別件（Claude Code系など）を返す
```

**改善提案**:
- 調査: `fetch_relevant_messages(scope=guild, limit=20, ...)` の結果順序確認
  - Semantic search のスコアが不適切でない確認
  - Recent timestamp による overrank を検討
- 修正: Recent 3-5 件の会話を必ず先頭に含める強制ロジック追加
- コード修正箇所: `src/main_agent/core/memory.py` lines ~300-350

---

### BUG-007: append_sheet_row 入力形式エラー（計画との不一致）

- **ステータス**: Fixed
- **優先度**: High
- **テスト ID**: UT-061
- **検出日時**: 2026-04-02 22:34
- **根本原因**: `append_sheet_row` アクションの `column_data` パラメータ仕様が計画と実装で不一致
  - 計画: `column_data` は **配列** `["2026-04-02", "テスト"]` を期待
  - 実装: `column_data` は **オブジェクト** `{"col1": "value1", "col2": "value2"}` として実装されている可能性
  - エラーメッセージ: `{"code": "invalid_column_data"}`

**再現手順**:
```
POST /debug_action
{
  "action": "append_sheet_row",
  "payload_json": "{\"sheet_name\":\"データ\",\"column_data\":[\"2026-04-02\",\"テスト\"]}"
}
期待: 行追記成功 → {"status": "ok"}
実際: {"code": "invalid_column_data"}
```

**改善提案**:
- 方針 A: 実装を計画に合わせる（配列対応）
  - 修正: `_handle_append_sheet_row` で配列を受け取り処理
- 方針 B: 計画を実装に合わせる（オブジェクト仕様に統一）
  - 修正: `docs/DESIGN.md` を更新し、期待値を変更
- 推奨: 方針 A（計画優先）
- コード修正箇所: `src/tools/action_tools.py` lines ~800-850

---

### BUG-008: Force Research Job 判定で timeout_sec 上限超過

- **ステータス**: Fixed
- **優先度**: High
- **テスト ID**: QLP-005
- **検出日時**: 2026-04-02 20:36
- **根本原因**: クエリで「1800秒でじっくり調べて」と指定した場合、dispatch_research_job ツール結果の `timeout_sec` が 1830 となり、期待値 1800 と不一致
  - 推定原因: Research Agent への HTTP リクエストで timeout 値を加工（padding or rounding）している可能性

**再現手順**:
```
Q: "1800秒でじっくり調べて"
期待: timeout_sec = 1800
実装判定結果: timeout_sec = 1830
```

**改善提案**:
- 調査: `dispatch_research_job()` 実装で `timeout_sec` 値の加工ロジック確認
  - コード: `src/tools/research_tools.py`
  - API 側: `src/research_agent/research_agent_server.py`
- 修正: 指定値を正確に伝達するか、明確な上限ルールを文書化

---

## Medium 優先度（部分的な動作不全）

### BUG-009: URL Reader 出力品質 - レート制限時の部分取得

- **ステータス**: Fixed
- **優先度**: Medium
- **テスト ID**: MT-015（参考）
- **検出日時**: 2026-04-02 20:10
- **根本原因**: `read_url_markdown()` 実行時に外部 API（DuckDuckGo など）がレート制限に達した場合、部分的な結果が返される
  - 現象不良: 完全な本文取得ではなく、途中で切れた結果を返す可能性
  - 影響: URL 要約の精度低下

**再現手順**:
```
1. 高速で複数 URL を read_url_markdown で処理
2. DuckDuckGo / web_search レート制限に達する
3. 返り値: {"status": "partial"} または 不完全な body
```

**改善提案**:
- リトライロジック強化: exponential backoff を実装
- または フォールバック: レート制限時は別プロバイダへ自動切替
- コード修正箇所: `src/tools/search_tools.py`

---

### BUG-010: Memory Retrieval - 無関係履歴混入（一般知識質問）

- **ステータス**: Fixed
- **優先度**: Medium
- **テスト ID**: CT-020
- **検出日時**: 2026-04-02 23:55
- **根本原因**: 一般知識質問「Python の標準ライブラリは？」に対して、無関係の過去履歴「私は寿司が好き」が context に混入
  - 原因: Semantic search の相似度判定が低いスコアでもヒット扱いしている可能性

**再現手順**:
```
1. 事前: 過去会話に "私は寿司が好き" を記録
2. Q: /ask "Python の標準ライブラリは？"
   期待: システム知識に基づく標準ライブラリ説明
   実際: Python 関連の履歴と共に "私は寿司が好き" が prompt context へ混入
```

**改善提案**:
- Semantic search の similarity threshold を厳格化（現在値 → より高い値への調整）
- または 一般知識質問を自動検出し、memory retrieval をスキップ
- コード修正箇所: `src/main_agent/core/memory.py`

**検証状況（2026-04-03 更新）**:
- 実装レベル再検証で、一般知識質問時の履歴取得が抑止されることを確認済み（`fetch_relevant_messages=0`）。
- Discord 実機でも `/ask Python の標準ライブラリは？` が `respond -> self-review approve` で完了し、履歴混入の再発がないことを確認。

**実機クローズ条件**:
- CT-020 を Discord 実機で再実施し、無関係履歴が回答に混入しないことを確認。
- `docs/TEST_RESULTS.md` の CT-020 を `PASS`（実機）に更新できること。

---

### BUG-011: Follow-up 判定ロジック（3段階以上の多段フォローアップ）

- **ステータス**: Fixed
- **優先度**: Medium
- **テスト ID**: MT-021, QLP-010, QLP-011, QLP-012
- **検出日時**: 2026-04-02 20:39
- **根本原因**: 3段階目以降のフォローアップで指示語参照先が喪失される
  - 例: Q1→Q2 の follow-up は成功、Q2→Q3 で参照先が曖昧化して別話題へ遷移

**再現手順**:
```
Q1: "Python async/awaitを3点で"
Q2: "2つ目だけ詳しく"（Q1 参照 ✓）
Q3: "じゃあサンプルコード"（Q2 参照を期待、実際は別話題へ）
```

**改善提案**:
- Context stack の拡張: 最新 3段階分の Q-A ペアを全て保持
- または 指示語解決の重点化: 各段階で「2つ目」→ Q2 の「2つ目」でなく Q1 の「3点中2つ目」へ逆参照可能なロジック
- コード修正箇所: `src/main_agent/core/orchestrator.py`

---

### BUG-012: Research Job 投入タイミングの遅延判定

- **ステータス**: Fixed（2026-04-03 再修正）
- **優先度**: Medium
- **テスト ID**: MT-026（参考）
- **検出日時**: 2026-04-02 20:11
- **根本原因**: URL 比較質問「 FastAPI と Flask の違い」で Reader 優先が期待されたが、Research Agent へ dispatch_research_job が選択される
  - 影響: URL 直接読み込みより遅い Research 経由になる
  - 原因推定: Tool 選択ロジックで research_job の優先度が高すぎる

**再現手順**:
```
Q: "https://fastapi.tiangolo.com/ と https://flask.palletsprojects.com/ の主張の違いを比較して"
期待: read_url_markdown × 2 → 比較
実際: dispatch_research_job へ委譲
```

**改善提案**:
- Tool 選択ロジック改善: URL が明示されている場合は Reader 優先
- コード修正箇所: `orchestrator._select_tool_for_question()`

---

## Low 優先度（設計上の制限 / 拡張機能）

### BUG-013: メッセージ添付長の判定精度

- **ステータス**: Fixed
- **優先度**: Low
- **テスト ID**: MT-002（参考）
- **検出日時**: 2026-04-02 22:44
- **観測内容**: 「Pythonの詳細解説を1000字以上で」という要求に対して、内部整形エラー調の応答が返される
- **根本原因**: 長文応答時の attachment mode への切替判定が精密でない可能性
- **改善提案**: `send_response()` の文字列長閾値（現在1900字）の見直し
  - 失敗文言の非最終応答判定を追加し、内部整形エラーがユーザーに見えないようにした
  - 実行時検証では、該当文言が `_is_nonfinal_response()` で True になることを確認済み

---

### BUG-014: Googleカレンダー更新系ツール未実装（タスク延期/更新）

- **ステータス**: Fixed
- **優先度**: High
- **テスト ID**: なし（運用ログで検出）
- **検出日時**: 2026-04-03 13:29
- **根本原因**: `execute_internal_action()` に `update_task` や Google Calendar の更新系アクションが実装されていない
  - 観測ログでは「レポート提出タスクの期限を2026年4月5日に更新」という意図で `update_task` が選ばれたが、`unsupported_action` で拒否された
  - その後、更新ではなく `add_task` を新規作成して代替しており、既存タスクの延期・更新という期待と異なる動作になっている
  - 影響: タスクや予定の修正ができず、重複登録や期限不整合を招く

**再現手順**:
```
1. /ask で既存タスクの期限延期や予定更新を依頼する
   期待: 既存タスク/予定の更新
   実際: execute_internal_action("update_task", ...) が unsupported_action を返す
2. その後、代替として add_task が実行される
   実際: 更新ではなく新規タスクが作成される
```

**改善提案**:
- 更新系アクションを追加する
  - 例: `update_task`, `update_calendar_event`, `reschedule_task`
- `execute_internal_action()` の許可アクション一覧とハンドラを拡張し、更新対象の ID ベース更新を実装する
- 失敗時に新規作成へ自動フォールバックせず、明示的に「未実装」と返す方針へ統一する
- コード修正箇所: `src/tools/action_tools.py`

**対応結果**:
- `update_task` を追加し、既存タスクの title / due_date 更新を実装
- `delete_task` も同じ経路で実装し、ID 解決後に削除できるようにした
- モック検証で `GET -> DELETE` の順に呼ばれることを確認済み

---

## 統計集計

### テスト結果対応表

| 不具合 ID | 関連テスト ID | 判定 | 原因カテゴリ |
|---|---|---|---|
| BUG-001 | MT-014, MT-016, MT-033 | Fixed | 判定ロジック不完全 |
| BUG-002 | CT-001, UT-004, UT-005, MT-017, MT-021, QLP-007, QLP-008, QLP-010, QLP-011, QLP-012 | Fixed | Context ドリフト |
| BUG-003 | CT-010 | Fixed | Calendar 統合エラー |
| BUG-004 | CT-004, CT-006 | Pending Verification | Callback 観測不可 |
| BUG-005 | CT-026, QLP-012 | Fixed | 指示語判定 |
| BUG-006 | QLP-007, QLP-008 | Fixed | Memory 精度 |
| BUG-007 | UT-061 | Fixed | API 仕様不一致 |
| BUG-008 | QLP-005 | Fixed | Timeout 加工 |
| BUG-009 | MT-015 | PASS（部分) | Reader レート制限 |
| BUG-010 | CT-020 | Fixed | Memory Retrieval 品質 |
| BUG-011 | MT-021, QLP-010, QLP-011, QLP-012 | Fixed | 多段 Follow-up |
| BUG-012 | MT-026 | Fixed | Tool 選択優先度 |
| BUG-013 | MT-002 | Fixed | 長文応答判定 |
| BUG-014 | 運用ログ | Fixed | 更新系アクション未実装 |

### カテゴリ別集計

| 原因カテゴリ | 不具合件数 | テスト失敗件数 |
|---|---|---|
| Context ドリフト / Follow-up | 5 件 | ~20 |
| Research/Tool 判定ロジック | 3 件 | 6 |
| API/実装 不一致 | 3 件 | 4 |
| Reader/Memory 品質 | 2 件 | 3 |
| 設計上の制限/拡張機能 | 1 件 | 1 |
| **合計** | **14 件** | **~35 件** |

---

## 修正優先順

1. **BUG-004**（Discord callback 観測不可）→ CT-006 実機確認を残して最終クローズ
2. **BUG-009**（URL Reader 部分取得）→ レート制限時の品質改善

---

## 更新履歴

| 日時 | 変更内容 |
|---|---|
| 2026-04-03 | 初版作成（BUG-001 〜 BUG-013、13件登録） |
| 2026-04-03 | BUG-014 を追加（Googleカレンダー更新系アクション未実装） |
| 2026-04-03 | BUG-001/002/003/005/006/007/008/011/014 を修正済みに更新 |
