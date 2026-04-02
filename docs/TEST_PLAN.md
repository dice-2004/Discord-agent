# テスト項目書

本書は [docs/DESIGN.md](docs/DESIGN.md) と [docs/change_log.md](docs/change_log.md) をもとに、現在実装できている機能を単体・複合の両面から確認するためのテスト項目をまとめたものです。

## 0. 実施前提と初期化手順

### 0.1 前提条件

- 対象は現在の実装だけに限定し、仕様書や変更履歴にあるが未実装の機能は含めない。
- Discord の許可ギルド、Research Agent、必要な `.env` 設定が反映済みであること。
- デバッグ系コマンドは `DEBUG_OPERATOR_USER_IDS` に登録されたユーザーで実施すること。
- 内部アクション系は `INTERNAL_ALLOWED_ACTIONS` と各認証情報の有無で結果が変わるため、成功系と失敗系の両方を確認する。
- Research Agent 系は `RESEARCH_AGENT_SHARED_TOKEN` と `RESEARCH_AGENT_URL` が正しいことを前提にする。

### 0.2 ベクターDB (ChromaDB) 初期化手順

テスト開始前に、メモリの学習状態をリセットしてクリーンな状態から開始することを推奨。手順：

```bash
# 1. Bot を停止
docker compose down

# 2. ChromaDB のデータ削除（メモリをリセット）
rm -rf data/chromadb/*

# 3. Bot を再起動（バックフィルをスキップ）
# 初回起動時は `MESSAGE_CONTENT_INTENT` が無効の場合、履歴取り込みがスキップされる
docker compose up main-agent research-agent -d

# 4. 起動を確認（数秒待機）
sleep 5
docker compose logs main-agent | grep "Logged in as"

# 5. チャンネル内にテスト用メッセージを数件残しておく（バックフィル用）
# Discord でメッセージを送信
```

テスト完了後、実環境の既存学習状態を復元したい場合は、git で `data/chromadb/` を復元してください。

## 1. 単体テスト項目

### 1.1 Discord 入力・応答

| ID | 機能 | 送信メッセージ例 | 手順 | 期待結果 |
|---|---|---|---|---|
| UT-001 | `/ask` の通常応答 | `/ask 今日の天気は？` | 許可ギルドで実行 | Bot が天気情報を回答する |
| UT-002 | `/ask` のギルド制限 | `/ask こんにちは` | 許可外ギルドで実行 | 「このサーバーではこのBotを利用できません」と返る |
| UT-003 | `/ask` の長文添付切替 | `/ask Pythonの詳細解説を1000字以上で` | 長文回答が出る質問 | 本文要約 + `ask_response.txt` 添付 |
| UT-004 | `/ask` と履歴参照 | Q1: `/ask Pythonについて` → Q2: `/ask さっきの内容をもう一度` | 直前会話ありの状態 | Q2 の回答が Q1 を参照している |
| UT-005 | `/ask` のフォローアップ解決 | Q1の回答が「・方法1 ・方法2 ・方法3」なら Q2: `/ask その3つの利点は？` | 列挙系の直後 | 指示語「その3つ」が 1-3 を指している |
| UT-006 | メンション応答 | をメンションして：`@bot 今年のトレンドは？` | Bot をメンション | `/ask` と同等の応答が得られる |
| UT-007 | メンション + 履歴参照 | Q1でメンション質問 → Q2: メンションして「前回の話を」 | メンションで連続質問 | Q2 が Q1 を参照している |

### 1.2 メモリ・プロフィール・セマンティック検索

| ID | 機能 | 送信メッセージ例 | 手順 | 期待結果 |
|---|---|---|---|---|
| UT-008 | 起動時バックフィル | 事前にチャンネルに複数メッセージを残しておく | Bot 起動 | 過去メッセージがメモリに取り込まれる |
| UT-009 | `on_message` 保存 | Bot 起動後、新規メッセージを送信 | メッセージ送信 | メッセージが継続保存される（後の検索で復帰） |
| UT-010 | guild 範囲のメモリ参照 | チャンネルA で「Pythonについて」、チャンネルB で `/ask Pythonの使い方は` | 異なるチャンネルで質問 | チャンネルA の記録も参照されている |
| UT-011 | ベクター検索（セマンティック） | チャンネルに「気温が高い日だ」と投稿 → `/logsearch 暑い` | logsearch で類似表現検索 | 「気温が高い」がヒットする（キーワード完全一致でなく） |
| UT-012 | 方向付きメモリ境界 | `DIRECTIONAL_MEMORY_ENABLED=true` で個人ギルドから身内ギルド記録を参照 | 許可方向の参照 | 個人 → 身内は OK、身内 → 個人は拒否 |
| UT-013 | メモリ証跡表示 | `MEMORY_RESPONSE_INCLUDE_EVIDENCE=true` で `/ask` を実行 | 回答生成 | 回答末尾に参照メモリ出典が表示される |
| UT-014 | `/memory_status` | `/memory_status` | 実行 | ギルド別記録件数と主要コレクション件数が表示 |
| UT-015 | `/profile_set` | `/profile_set 得意分野 Python機械学習` | キー・値を指定 | 項目が保存または更新される |
| UT-016 | `/profile_set` の検証 | `/profile_set x ""` （空値） | 検証失敗をテスト | バリデーションエラー |
| UT-017 | `/profile_show` | `/profile_show` | 事前に `/profile_set` で項目保存 | 保存済み項目が一覧表示 |
| UT-018 | `/profile_forget` 単項目 | `/profile_forget 得意分野` | 指定項目削除 | その項目だけ削除される |
| UT-019 | `/profile_forget` 全削除 | `/profile_forget` （キーなし） | 全削除実行 | プロファイルが全削除される |

### 1.3 Research Agent・検索・GitHub 読み込み内部機能

| ID | 機能 | 送信メッセージ例 | 手順 | 期待結果 |
|---|---|---|---|---|
| UT-020 | `/deepdive` の Research Agent 委譲 | `/deepdive kubernetes 深掘りして調べてきて` `DEEPDIVE_USE_RESEARCH_AGENT=true` | Research Agent 有効状態 | dispatch_research_job でジョブ投入される |
| UT-021 | `/deepdive` の直接深掘り | `/deepdive redis` `DEEPDIVE_USE_RESEARCH_AGENT=false` | Research Agent 無効 | source_deep_dive が直接実行される |
| UT-022 | GitHub repo URL の自動検出と README 読み込み | `/ask https://github.com/google/go-github について教えて` | GitHub URL を含む質問 | README が取得され、内容が回答に反映される |
| UT-023 | GitHub About（description）の読み込み | `/ask authlib の説明をして` | GitHub リポジトリ名を含む質問 | リポジトリの description が参照される |
| UT-024 | KC3Hack 2025 履歴検索テスト | `/logsearch KC3Hack 2025 scope:guild` | Discord 過去会話（ベクターDB）から検索 | KC3Hack 2025 の募集案内・予算関連投稿がヒットする |
| UT-025 | `/readurl` | `/readurl https://docs.python.org/3/library/asyncio.html` | URL 指定 | URL 本文が Markdown で抽出・返答される |
| UT-026 | `/logsearch` の channel スコープ | `/logsearch Python scope:channel` | channel 指定 | 現在のチャンネル中心の候補が返る |
| UT-027 | `/logsearch` の guild スコープ | `/logsearch Python scope:guild` | guild 指定 | 同一ギルド全体の候補が返る |
| UT-028 | `/logsearch` のスコア表示 | `LOGSEARCH_INCLUDE_SCORE=true` で `/logsearch` 実行 | スコア表示有効 | 一致度・新しさ・総合スコアが表示される |
| UT-029 | `/auth_status` | `/auth_status` | 実行 | GitHub / Calendar / SMTP の認証状態と導線 URL が表示 |

### 1.4 CLI・監査・デバッグ

| ID | 機能 | 送信メッセージ例 | 手順 | 期待結果 |
|---|---|---|---|---|
| UT-030 | `/runcli` のリクエスト | `/runcli docker ps` | 許可済みコマンドで実行 | 承認ボタン付きリクエストが投稿される |
| UT-031 | `/runcli` のボタン承認 | approve ボタンを押す | 承認者が操作 | コマンドが実行され、監査ログに残る |
| UT-032 | `/runcli` のボタン拒否 | reject ボタンを押す | 承認者が操作 | 実行されず、拒否ログが残る |
| UT-033 | `/runcli` の権限制限 | 非承認者が approve/reject | 権限なしで操作 | 「権限がありません」エラー |
| UT-034 | `/runcli_audit` 既定表示 | `/runcli_audit` | 実行 | 直近の監査イベント（最大20件デフォルト） |
| UT-035 | `/runcli_audit` event フィルタ | `/runcli_audit event:approved` | イベント絞り込み | 指定イベントのみ表示 |
| UT-036 | `/debug_action` の権限制限 | 非デバッグ担当者が `/debug_action` | 権限なしで実行 | 利用拒否 |
| UT-037 | `/debug_action` の実行 | `/debug_action add_task payload_json: {"title":"テスト"}` | 許可済み action | JSON 結果が返る |
| UT-038 | `/debug_mention_probe` | `/debug_mention_probe こんにちは` | デバッグ担当者が実行 | Bot が投稿・応答がチャンネルに残る |
| UT-039 | `/debug_probe_tail` | `/debug_probe_tail` | 実行 | 最新の probe 監査ログが表示 |

### 1.5 Research Agent API・ジョブ連携

| ID | 機能 | HTTP 例 | 期待結果 |
|---|---|---|---|
| UT-040 | `/healthz` | `GET http://research-agent:8091/healthz` | `{"status":"ok","service":"research-agent"}` |
| UT-041 | `POST /v1/jobs` 正常 | `POST /v1/jobs` body: `{"topic":"Python","source":"auto"}` | `201` で `{"status":"queued","job_id":"rj-..."}` |
| UT-042 | `POST /v1/jobs` エラー | `POST /v1/jobs` body: `{"topic":""}` | `400` で `{"code":"invalid_topic"}` |
| UT-043 | `GET /v1/jobs/{id}` 正常 | `GET /v1/jobs/rj-1234-5678` | `200` で job 情報（engine・report・decision_log を含む） |
| UT-044 | `GET /v1/jobs/{id}` 未存在 | `GET /v1/jobs/rj-notexist` | `404` で `{"code":"job_not_found"}` |
| UT-045 | `dispatch_research_job` wait=false | wait=false パラメータ | `queued` のまま返り、背景処理継続 |
| UT-046 | `dispatch_research_job` wait=true | wait=true パラメータ | 完了なら最終レポート、未完了なら継続メッセージ |
| UT-047 | `dispatch_research_job` mode=auto | mode=auto パラメータ | CLI 可能時は CLI 先行の結果 |
| UT-048 | `dispatch_research_job` mode=gemini_cli | mode=gemini_cli パラメータ | CLI のみで完了、失敗時はエラー |
| UT-049 | `dispatch_research_job` mode=fallback | mode=fallback パラメータ | 管理AI優先で処理 |
| UT-050 | `get_research_job_status` 正常 | `job_id=rj-1234` | 状態 JSON が返る |
| UT-051 | `get_research_job_status` エラー | `job_id=""` | `{"code":"invalid_job_id"}` |

### 1.6 内部アクション

| ID | 機能 | 送信例・パラメータ | 期待結果 |
|---|---|---|---|
| UT-052 | 未対応 action | `action: unknown_action` | `{"code":"unsupported_action"}` |
| UT-053 | 無効 payload_json | `payload_json: "not json"` | `{"code":"invalid_payload_json"}` |
| UT-054 | action 別名正規化 | `action: calendar_add_event` → `add_calendar_event` に変換 | 正規化されて処理される |
| UT-055 | `add_calendar_event` (timed) | `title:"会議",start_time:"2026-04-10T14:00",end_time:"2026-04-10T15:00"` | 予定が保存 |
| UT-056 | `add_calendar_event` (all-day) | `title:"誕生日",all_day:true,date:"2026-04-10"` | 終日予定が保存 |
| UT-057 | `add_calendar_event` 必須不足 | `title: "会議"` （時刻なし） | `{"code":"missing_required_fields"}` |
| UT-058 | `get_calendar_events` | `time_min:"2026-04-01T00:00",time_max:"2026-04-30T23:59"` | 該当期間の予定一覧 |
| UT-059 | `add_task` | `title:"レポート提出"` | タスク保存 |
| UT-060 | `add_notion_memo` | `title:"AI",content:"メモ",category:"tech"` | Notion メモ保存 |
| UT-061 | `append_sheet_row` | `sheet_name:"データ",column_data:["2026-04-02","テスト"]` | CSV へ1行追記 |
| UT-062 | `create_github_issue` 未認証 | `GITHUB_TOKEN` 未設定で実行 | `{"code":"auth_required","auth_url":"..."}` |
| UT-063 | `create_github_issue` 成功 | `GITHUB_TOKEN` 済みで `repository:"owner/repo",title:"bug"` | Issue 作成成功 |
| UT-064 | `backup_server_data` 許可パス | `target:"/home/user/data"` （許可リスト内） | `.tar.gz` 作成 |
| UT-065 | `backup_server_data` 禁止パス | `target:"/etc"` （許可外） | 実行拒否 |
| UT-066 | `send_email` 未認証 | SMTP 情報なしで実行 | `{"code":"auth_required"}` |
| UT-067 | `send_email` 成功 | SMTP 済み、`to_address:"test@example.com"` | メール送信成功 |

## 2. 複合テスト項目

| ID | 組み合わせ | 具体手順 | 期待結果 |
|---|---|---|---|
| CT-001 | `/ask` + 履歴参照 + メモリ | Q1: `/ask Python」→ Q2: `/ask さっきの話をもっと詳しく` | Q2 が Q1 の内容を踏まえた回答をしている |
| CT-002 | `/ask` + follow-up + 長文 | Q1 の回答が列挙 → Q2: `/ask その全部を数百字で詳しく` | 指示語正解 + 長文で添付切替 |
| CT-003 | `/ask` + Research 自動委譲 | `/ask 最新の AI トレンドを詳しく調べてきて` | dispatch_research_job が実行され、背景通知で結果返却 |
| CT-004 | Research ジョブ + 通知 | `/deepdive kubernetes` で queued 状態 | 後続で同チャンネルに「調査完了」通知が投稿される |
| CT-005 | `/ask` + persona memory | `/profile_set スタイル 簡潔にお願い`  → `/ask 応答されたい流儀` | 保存した「簡潔」が回答に反映される |
| CT-006 | `/deepdive` + polling + 通知 | `DEEPDIVE_USE_RESEARCH_AGENT=true` で `/deepdive` 実行 | ジョブ投入 → 定期ポーリング → 最終通知が通る |
| CT-007 | `/runcli` + audit 追跡 | コマンド承認 → `/runcli_audit` | request/approved/executed が一連で追跡できる |
| CT-008 | bot 自律メンション + audit | `/debug_mention_probe こんにちは` | チャンネル投稿 + 自動応答 + audit 記録が全部揃っている |
| CT-009 | action 失敗 + auth_status | `create_github_issue` を GITHUB_TOKEN なしで実行 → `/auth_status` | 失敗理由と認証 URL が両方確認できる |
| CT-010 | calendar 操作 + 質問参照 | `/debug_action add_calendar_event ...` で追加 → `/ask 今週の予定は？` | 追加済み予定が自然文で参照される |
| CT-011 | プロファイル CRUD + 応答反映 | `/profile_set` → `/profile_show` → `/ask` | 保存・確認・反映の全サイクル |
| CT-012 | バックフィル + logsearch + ask | チャンネル既存メッセージ → Bot 起動 → `/logsearch` → `/ask` | 過去ログが検索でき、応答にも反映 |
| CT-013 | GitHub 深掘り + README/About 分離 | `/ask github.com/owner/repo について` | README/About が別々に正しく参照される |
| CT-014 | KC3Hack 履歴文脈テスト | `/ask KC3Hack 2025 の過去案内を要約して` | Discord 過去会話由来の情報（募集案内・締切・予算）を参照して要約できる |

## 3. 文脈理解・文脈非参照テスト（重要）

### 3.1 文脈理解テスト（セマンティック検索の確認）

| ID | シナリオ | 送信メッセージ | チャンネル記録 | 期待結果 |
|---|---|---|---|---|
| CT-015 | セマンティック検索 | `/logsearch 暑い` | チャンネルに「気温が高くて不快だ」という投稿あり | 「気温が高い」がヒットする（完全一致でなく） |
| CT-016 | セマンティック検索 2 | `/logsearch コンピュータ` | チャンネルに「PC でプログラミング」という投稿あり | 「コンピュータ」より「コンピュータ」に意味的に近い記録がヒット |
| CT-017 | 文脈参照する質問 | `/ask 最近話した内容で、何が印象的だった？` | チャンネルに複数の会話履歴あり | 複数履歴から文脈的に重要な内容が選ばれて言及される |
| CT-018 | 類似トピック検索 | `/ask プログラムしたか？` | チャンネルに「Pythonコード書いた」という投稿あり | 「プログラミング」と「Pythonコード」の類似性で参照 |

### 3.2 文脈非参照テスト（明示的グローバルクエリ）

| ID | 機能 | 送信メッセージ例 | チャンネル記録 | 期待結果 |
|---|---|---|---|---|
| CT-019 | 新規情報明示（グローバルクエリ） | `/ask 最新の AI トレンド 2026 について教えて` | 事前に「AI について」という記録あり | 最新情報を広くリサーチし、チャンネル履歴は参照しない |
| CT-020 | 一般知識質問 | `/ask Python の標準ライブラリは？` | チャンネルに Python 関連の記録あり | 一般知識なので、個人的な履歴参照せず、一般的な回答 |
| CT-021 | グローバル + フォローアップ混在 | `/ask GitHub について` （グローバル）→ `/ask それについてもっと詳しく` （フォローアップ） | 前後の記録 | 2つ目は前述「GitHub」の内容を踏まえている |
| CT-022 | 明示的グローバルなのにマーカーあり | `/ask 世界中で出来事・そのうち日本の話は？` | チャンネルに日本記事あり | グローバル指示なのに「その」が含まれるときのルール確認 |

### 3.3 フォローアップ検出・指示語解決テスト

| ID | 機能 | 手順 | チャンネル状態 | 期待結果 |
|---|---|---|---|---|
| CT-023 | 数字指示語「その3つ」 | Q1: `/ask プログラミング言語の選び方` → Q2: `/ask その3つのメリットは？` | Q1 が「Python・Go・Rust」と列挙 | Q2 の「その3つ」が正確に 1-3 を指している |
| CT-024 | 指示語「それぞれ」 | Q1: 複数項目列挙 → Q2: `/ask それぞれの特徴を` | Q1 の結果に複数項目 | 各項目の特徴が個別に説明される |
| CT-025 | 指示語「上記」 | Q1: 情報提示 → Q2: `/ask 上記内容を要約` | Q1 の記述内容 | Q2 が Q1 の内容を正確に要約 |
| CT-026 | 非フォローアップ「その」 | `/ask 「その料理は何ですか」という質問で` | 新規質問 | 「その」が指示語でなく通常語彙として扱われる |

### 3.4 DB実測シードに基づく過去文脈テスト（汎用）

以下は `data/chromadb/chroma.sqlite3` の実データから抽出したシード例を使う。机上文例ではなく、実際に保存済みの過去会話で検証する。

| ID | シナリオ | 送信メッセージ（@メンション） | DB由来シード例 | 期待結果 |
|---|---|---|---|---|
| CT-027 | 天気文脈の再利用 | `@bot 京都の天気の話、前にしてた内容も含めて要約して` | 「今日の京都の天気は？」「2026年3月31日、京都の天気は...」 | 直近質問だけでなく過去の天気会話を統合して回答 |
| CT-028 | 運用系文脈の再利用 | `@bot pingやサーバー状態の話って最近どうだった？` | 「ping」「Proxmoxサーバー...」 | 過去の運用トラブル文脈を拾って要約 |
| CT-029 | プロダクト文脈の再利用 | `@bot yorimichi の話、過去に出た要点をまとめて` | 「yorimichi 発表文」「yorimichi URL」 | 過去会話ベースで概要を再構成 |
| CT-030 | GitHub議論文脈の再利用 | `@bot thought_signature の話、前回の比較案を短く` | 「GitHub中心に調べて」「実装案を3つ比較」 | 以前の比較観点を引き継いだ要約が返る |

### 3.5 任意シード（KC3Hack を使う場合）

KC3Hack を使いたい場合のみ追加で実施する。必須ではない。

| ID | シナリオ | 送信メッセージ（@メンション） | DB由来シード例 | 期待結果 |
|---|---|---|---|---|
| CT-031 | 正規表記の過去会話想起 | `@bot KC3Hack 2025 の案内って過去に何が流れてた？` | 「KC3Hack 2025 プレエントリー開始」「本エントリー受付中」投稿 | 募集案内の主旨・締切文脈を要約して返す |
| CT-032 | 誤記ゆらぎの吸収 | `@bot kc3hacl 2025 の募集案内あったよね？` | DB上は `kc3hack 2025` 表記のみ（`kc3hacl` は0件） | typoでも `KC3Hack 2025` を候補として拾い、関連履歴に寄せて回答 |

## 4. @メンション中心の同等性テスト（既存コマンド試験は維持）

方針:
- 既存の `/ask`, `/deepdive`, `/logsearch` などのコマンド試験はそのまま残す。
- 追加で、同等ユースケースを `@bot ...` でも実施し、同等品質で応答できるか確認する。
- メッセージ本文では「deepdiveを使え」「リサーチエージェントを使え」などの実装指示はしない。必要な内部手段はエージェント判断に任せる。

| ID | コマンド系の対応元 | @メンション送信例 | 検証観点 | 期待結果 |
|---|---|---|---|---|
| MT-001 | UT-001（通常質問） | `@bot 今日の京都の天気は？` | 通常QA | `/ask` 相当の通常応答 |
| MT-002 | UT-003（長文切替） | `@bot Pythonの詳細を1000字くらいで整理して` | 長文応答 | 長文時の分割/添付ポリシーが維持される |
| MT-003 | UT-004（直前履歴） | Q1: `@bot Pythonの特徴` → Q2: `@bot さっきの話をもう一度` | 履歴参照 | Q2 がQ1の内容を引き継ぐ |
| MT-004 | UT-005（指示語解決） | Q1: `@bot 学習方法を3つ` → Q2: `@bot その3つの利点は？` | フォローアップ | 指示語解決が崩れない |
| MT-005 | UT-011（セマンティック検索相当） | `@bot 暑い日に関する最近の話あった？` | 意味検索 | 「気温が高い」等の類義履歴を参照 |
| MT-006 | UT-010（guild横断文脈） | 別チャンネルで話題投入後に `@bot Pythonの使い方は？` | スコープ解決 | 同一ギルド内の関連履歴を参照 |
| MT-007 | CT-027（天気の履歴統合） | `@bot 京都の天気、前回と今回をまとめて` | 実測DB文脈利用 | 過去と現在の情報を整理して返す |
| MT-008 | CT-028（運用ログ文脈） | `@bot pingやProxmoxの件、前回の結論は？` | 実測DB文脈利用 | 運用系会話の要点を再提示 |
| MT-009 | CT-029（プロダクト文脈） | `@bot yorimichiって前にどんな説明だった？` | 実測DB文脈利用 | 過去説明を踏まえた要点整理 |
| MT-010 | CT-030（GitHub比較案の再利用） | `@bot thought_signature の比較案を再掲して` | 実測DB文脈利用 | 過去の比較軸を維持して回答 |
| MT-011 | UT-026/027（検索スコープ） | `@bot このチャンネル中心で、Pythonの話を要約して` | 暗黙スコープ | チャンネル優先の回答傾向 |
| MT-012 | UT-029（認証系導線） | `@bot GitHub連携の状態を教えて` | ツール利用判断 | 状態説明と必要なら導線提示 |
| MT-013 | CT-003/004（深掘り系質問） | `@bot 最新のAIトレンドを詳しく調べて` | 内部委譲判断 | 深掘りが必要な問いで内部的に調査系フローが動く |
| MT-014 | deepdive相当2 | `@bot Gemini APIのthought_signatureエラー対策を比較して` | 内部委譲判断 | 比較・根拠付きの詳細回答が返る |
| MT-015 | deepdive相当3 | `@bot GitHub中心で最新の議論を調べて要点だけ` | 内部委譲判断 | 必要時に調査系フローで結果が返る |
| MT-016 | deepdive相当4 | `@bot Kubernetes運用の失敗例を調べて対策を3つ` | 内部委譲判断 | 具体的な調査結果 + 施策提案 |
| MT-017 | follow-up + deepdive相当 | Q1: `@bot AIトレンドを調べて` → Q2: `@bot その中で実装優先度を付けて` | 継続文脈 | Q1結果を踏まえた優先度整理 |
| MT-018 | 文脈非参照（新規化） | `@bot 最新のAIニュースだけ教えて。過去会話は使わないで` | 参照抑制 | 過去会話依存を抑えた回答 |
| MT-019 | 文脈非参照（グローバル） | `@bot site:github.com で今週の話題を見て` | 参照抑制 | グローバル調査寄りの回答 |
| MT-020 | 誤記ゆらぎ | `@bot kc3hacl 2025 の話って前にあった？` | typo耐性 | `kc3hack` 系履歴に寄せて回答 |
| MT-021 | 連続3ターン検証 | Q1: `@bot Python async/awaitを3点で` → Q2: `@bot 2つ目だけ詳しく` → Q3: `@bot じゃあサンプルコード` | 多段追跡 | ターン間整合性が維持される |
| MT-022 | メンション前置必須系 | 文中メンション: `今日は <@bot> どう思う？` | prefix制約 | `MENTION_REQUIRE_PREFIX=true` 時は反応しない |
| MT-023 | 空入力 | `@bot` のみ送信 | 入力検証 | 「メンションの後ろに質問内容を書いてください。」が返る |
| MT-024 | 監査確認 | `@bot こんにちは` 後に `/debug_probe_tail` | 監査証跡 | mention_answer_sent が監査に残る |
| MT-025 | Tool2 Reader相当1（単一URL要約） | `@bot https://docs.python.org/3/library/asyncio.html の内容を3行で要約して` | Reader起動判断 | URL本文読解ベースの要約が返る |
| MT-026 | Tool2 Reader相当2（複数URL比較） | `@bot https://fastapi.tiangolo.com/ と https://flask.palletsprojects.com/en/stable/ の主張の違いを比較して` | Reader複数回判断 | 各URL本文を読んだ比較結果が返る |
| MT-027 | Tool2 Reader相当3（ノイズ除去確認） | `@bot このURLの本文だけ抜いて箇条書きにして https://developer.mozilla.org/ja/docs/Web/HTTP/Basics_of_HTTP` | Markdown抽出品質 | メニュー/広告でなく本文中心の要点になる |
| MT-028 | Tool2 Reader相当4（URL + 文脈） | Q1: `@bot この記事読んで https://fastapi.tiangolo.com/tutorial/` → Q2: `@bot さっきの記事の懸念点だけ` | Reader + follow-up | Q2がQ1で読んだ本文内容を前提に返る |
| MT-029 | Tool2 Reader相当5（失敗時耐性） | `@bot https://invalid.invalid/abc を読んで要約して` | Reader失敗ハンドリング | Bot全体は落ちず、安全な失敗メッセージで継続 |
| MT-030 | Tool3 深掘り相当1（GitHub議論） | `@bot LangChainの最近のIssue傾向を調べて要点だけ` | 特殊ソース深掘り判断 | GitHub由来の論点を要約して返る |
| MT-031 | Tool3 深掘り相当2（Reddit反応） | `@bot Python 3.13への反応をReddit中心に俯瞰して` | 特殊ソース深掘り判断 | SNS/コミュニティ反応を整理して返る |
| MT-032 | Tool3 深掘り相当3（YouTube/X横断） | `@bot このテーマの動画とSNS反応の違いを比較して` | 複数特殊ソース判断 | ソース別の観点差を示した比較回答 |
| MT-033 | Tool3 深掘り相当4（深掘り後フォローアップ） | Q1: `@bot Gemini API運用の実例を調べて` → Q2: `@bot その中で再現しやすい順に並べて` | 深掘り + follow-up | Q1調査結果を受けた再順位付け回答 |
| MT-034 | Tool2/3 境界判断 | `@bot https://docs.python.org/3/library/asyncio.html の要約と、関連コミュニティの反応も合わせて` | Reader + 深掘りの併用判断 | URL本文要約と外部反応の両方を統合した回答 |

## 5. 実施手順とチェックリスト

### 5.1 テスト実施の流れ

1. **初期化**（0.2）に従い ChromaDB をリセット  - ベクターDB が文脈理解することを確実にするため、必ず初期化から開始してください
2. **単体テスト**：§1 の UT-001 から UT-067 まで順実施
   - エラーが出たら、その時点でログとエラーメッセージを記録してください
3. **複合テスト**：§2 の CT-001 から CT-014 まで
   - 複数ステップに分かれているため、各ステップ間で結果を確認
4. **文脈理解テスト**：§3 の CT-015 から CT-026 まで
   - **特に重要**: セマンティック検索が本当に機能しているか、文脈を理解していないときが何かを確認
   - 見落としやすい部分なので、丁寧に進める
5. **DB実測文脈テスト**：§3.4 の CT-027 から CT-030 を実施
   - 実際のDiscord過去会話を回収できるか確認
6. **任意シードテスト**：§3.5 の CT-031 から CT-032 を実施（必要時のみ）
7. **@メンション同等テスト**：§4 の MT-001 から MT-034 を実施
   - コマンド系と同等以上の品質かを確認（手段指定せず）
8. **エラーハンドリングテスト**：§7 の ERR-001 から ERR-010 を実施
   - 異常系でBotが落ちないことを確認（仕様 §7.3 受け入れ条件）
9. **質問ロジックパステスト**：§8 の QLP-001 から QLP-023 を実施
   - Discord側からの質問送信で通過するすべてのコードパスを網羅的に確認
10. **セキュリティテスト**：§9 の SEC-001 から SEC-006 を実施
11. **設定バリデーションテスト**：§10 の CFG-001 から CFG-008 を実施
12. **エッジケース・境界値テスト**：§11 の EDGE-001 から EDGE-012 を実施

### 5.2 テスト記録フォーマット

各テスト実施後、以下を記録してください：

```
[ テスト ID ]
- 実施日時: YYYY-MM-DD HH:MM
- 送信メッセージ: (正確に記録)
- 観測結果: (実際に返ってきた応答)
- 期待結果: (仕様から期待される結果)
- 判定: PASS / FAIL / N/A
- 備考: (必要があれば)
```

例：
```
[ UT-004 ]
- 実施日時: 2026-04-02 14:30
- 送信メッセージ: [Q1] /ask Python について [Q2] /ask さっきの話をもっと詳しく
- 観測結果: Q2 の回答が Q1 で述べた「Pythonはシンプルで学習曲線が緩い」などの内容を踏まえていた
- 期待結果: 直前会話を参照した回答
- 判定: PASS
- 備考: 正常に動作
```

## 6. 重要な補足

- **セマンティック検索の確認**: CT-015～CT-018 は必ず実施し、ベクターDBが本当にセマンティック検索しているか確認してください。テスト中に不確実であれば、ChromaDB のコレクション内容を直接確認してもよいです
- **Discord過去会話の文脈理解（必須）**: CT-027〜CT-030 と MT-007〜MT-021 で、実際に保存された過去会話を根拠に回答できるか確認してください
- **KC3Hack は任意**: KC3Hack系は CT-031〜CT-032 と MT-020 の補助シナリオとして扱い、必須テストにはしないでください
- **GitHub README vs About**: UT-023 で README と About が正しく分離されているか確認してください
- **Tool2/Tool3 の重点確認**: MT-025〜MT-034 で、Reader（本文抽出）と特殊ソース深掘りの判断・品質・失敗耐性を重点検証してください
- **Tool3 の現行実装範囲**: GitHubは `source_deep_dive` 内でAPI probe（README/About/Issue/PR）を実施。Reddit/X/YouTubeは現時点では専用API連携ではなく `site:` 検索ベースであるため、テスト判定もこの実装差を前提にしてください

## 7. エラーハンドリング・耐障害性テスト

仕様 §7.3「以下でBotが落ちないこと」に対応する受け入れ条件テスト。

| ID | 障害シナリオ | 手順 | 期待結果 | 確認方法 |
|---|---|---|---|---|
| ERR-001 | LLM API キー無効 | `GEMINI_API_KEY` を無効な値にして `/ask` 実行 | Botが落ちず「AI応答で問題が発生」エラーメッセージを返す | 応答テキスト + `docker logs` でスタックトレース確認 |
| ERR-002 | LLM API タイムアウト | `GEMINI_TIMEOUT_SEC=1` など極端に短い値で `/ask` 実行 | リトライ後にエラーメッセージを返し、Bot継続 | ログに `Gemini invocation failed after retries` |
| ERR-003 | Web検索ツール失敗 | 外部ネットワーク遮断状態で `/ask 最近のニュースは？` | 検索失敗をscratchpadに記録し、それでも回答を生成 | ログに `Failed` 記録、応答は返る |
| ERR-004 | ChromaDB書き込み失敗 | ChromaDB パスを読み取り専用にしてメッセージ送信 | メッセージ保存失敗をログに記録し、Bot継続 | `docker logs` に例外出力、Botプロセス存続 |
| ERR-005 | Discord送信権限不足 | Botに送信権限のないチャンネルで `/ask` 実行 | followup.send の例外をキャッチ、Bot継続 | ログに例外、Botプロセス存続 |
| ERR-006 | 必須環境変数欠落 | `GEMINI_API_KEY` を空にして起動 | `RuntimeError` で安全に終了 | 起動ログに `GEMINI_API_KEY are required` |
| ERR-007 | `initial_profile.md` 不在 | ファイルを削除して起動 | WARNINGログ出力、プロファイル空で継続 | ログに `initial_profile.md not found` |
| ERR-008 | `initial_profile.md` 超過 | 12,000文字超のファイルを配置して起動 | 切り詰めてWARNING出力、Bot正常動作 | ログに `exceeds max chars` |
| ERR-009 | Research Agent 接続失敗 | `RESEARCH_AGENT_URL` を無効にして `/deepdive` 実行 | エラーメッセージを返し、Bot継続 | 「deep diveの実行に失敗しました」が返る |
| ERR-010 | ペルソナ記憶読み込み失敗 | ChromaDB のペルソナコレクションを破損させて `/ask` | ペルソナ取得失敗をログに記録、通常回答を返す | ログに `Failed to load persona profile`、応答は返る |

## 8. Discord質問ロジックパス網羅テスト

`/ask` および `@メンション` から質問が送信された際に通過するコード内の各ロジックパス（ルーティング分岐）を網羅的に確認するテスト。

### 8.1 Research Controls 注入パス

質問テキスト内の「Gemini CLI」「フォールバック」「〇〇秒」「〇〇分」の検出と、`[Research Controls]` ブロック注入（`_extract_research_controls` → `_inject_research_controls_hint_with_values`）を検証する。

| ID | シナリオ | 送信メッセージ | 期待結果 | 確認方法 |
|---|---|---|---|---|
| QLP-001 | mode=gemini_cli の自動検出 | `@bot Gemini CLIでAIトレンドを調べて` | `dispatch_research_job` の `mode` が `gemini_cli` になる | ログに `mode=gemini_cli` |
| QLP-002 | mode=fallback の自動検出 | `@bot フォールバックモードで最近のPython事情を調査して` | `dispatch_research_job` の `mode` が `fallback` になる | ログに `mode=fallback` |
| QLP-003 | timeout_sec の秒指定 | `@bot 120秒でKubernetesの最新動向を調べて` | `timeout_sec=120` がそのまま使用される | ログに `timeout_sec: 120` |
| QLP-004 | timeout_sec の分指定 | `@bot ２分間でRedditの反応を調べて` | 全角→半角変換 + 分→秒（120）変換 | ログに `timeout_sec: 120` |
| QLP-005 | timeout_sec の境界値（上限） | `@bot 1800秒でじっくり調べて` | `timeout_sec=1800`（上限値） | ログに `timeout_sec: 1800` |
| QLP-006 | mode + timeout の同時指定 | `@bot Gemini CLIで60秒でGitHubの議論を調べて` | `mode=gemini_cli` かつ `timeout_sec=60` | ログで両方確認 |

### 8.2 Recent Conversation 文脈組立パス

`_should_attach_recent_context` → `_build_recent_conversation_context` → `_inject_recent_conversation_hint` の一連の分岐を検証する。

| ID | シナリオ | 送信メッセージ | 期待結果 | 確認方法 |
|---|---|---|---|---|
| QLP-007 | recall質問でRecent Context が40件取得 | 直前にチャンネルで複数会話 → `/ask さっき話した内容は？` | `recent_limit=40` で広い文脈を取得して回答に反映 | 回答が直近の会話内容を踏まえている |
| QLP-008 | フォローアップ指示語でRecent Contextが8件取得 | Q1で質問 → `/ask それについてもう少し教えて` | `recent_limit=8` で直近文脈を取得 | Q1の内容を踏まえた回答 |
| QLP-009 | recall/followup でない新規質問 | `/ask 今日の天気は？`（新規質問、指示語なし） | Recent Context を**付与しない** | 過去会話に引きずられない独立した回答 |

### 8.3 フォローアップ解決・指示語注入パス

`_is_list_followup_query` → `_inject_followup_targets_hint` で列挙系フォローアップの解決を検証する。

| ID | シナリオ | 送信メッセージ | 期待結果 | 確認方法 |
|---|---|---|---|---|
| QLP-010 | 「その3つ」→ 列挙内容解決 | Q1: `/ask 推奨フレームワーク3つ` → Q2: `/ask その3つの違いは？` | `[Resolved Follow-up Context]` が注入され、Q1の3項目を参照 | Q2の回答がQ1の3項目を正確に参照 |
| QLP-011 | 「それぞれ」→ 列挙内容解決 | Q1: 複数項目応答 → Q2: `/ask それぞれの長所は？` | 各項目が個別に説明される | 各項目別の回答 |
| QLP-012 | 「深掘り」→ フォローアップ解決 | Q1: 概要回答 → Q2: `/ask 深掘りして` | Q1の内容を掘り下げた回答 | Q1の内容をベースにした詳細回答 |

### 8.4 曖昧クエリの検出・拒否パス

`_is_underspecified_external_research_query` のロジック分岐を検証する。曖昧な外部調査クエリでフォローアップマーカーがない場合に、調査対象が不明として拒否するパスを確認する。

| ID | シナリオ | 送信メッセージ | 期待結果 | 確認方法 |
|---|---|---|---|---|
| QLP-013 | 曖昧クエリ（対象なし + followupなし） | `/ask Githubの最新議論を調べて`（具体的なリポジトリ指定なし、直前文脈なし） | 「調査対象が曖昧です。どの対象の最新議論を調べるか指定してください」と返る | 明確化を求めるメッセージ |
| QLP-014 | 曖昧クエリ + フォローアップマーカー | Q1: `@bot oithxs/yorimichiについて` → Q2: `/ask その最新議論を調べて` | Q1のトピックを継承して調査実行 | Q2がQ1のリポジトリについて調査結果を返す |
| QLP-015 | 具体的クエリ（URL指定あり） | `/ask github.com/owner/repoの最新議論を調べて` | 曖昧判定をスキップし調査実行 | 具体的な調査結果が返る |

### 8.5 Research Job 強制ディスパッチパス

`_should_force_research_job` の判定ロジックを検証する。ソース特化用語 + 調査用語の組み合わせで、LLMの判断を待たずに強制的に `dispatch_research_job` を実行するパス。

| ID | シナリオ | 送信メッセージ | 期待結果 | 確認方法 |
|---|---|---|---|---|
| QLP-016 | ソース語 + 調査語 → 強制dispatch | `@bot Twitterの最新の反応を調べて` | `_should_force_research_job=true` → `dispatch_research_job` が実行される | ログに `force_dispatch_research_job` |
| QLP-017 | エンティティ検索意図 → 強制dispatch | `@bot yorimichiについて教えて` | エンティティルックアップ意図で `dispatch_research_job` が実行される | ログに `guard_external_research_intent` |
| QLP-018 | タスク/予定系は強制dispatchしない | `@bot 明日のyorimichiタスクを追加して` | 「タスク」キーワードで強制dispatch **無効**: `execute_internal_action` → `add_task` | タスクが正しく追加される |

### 8.6 Self-Review（自己レビュー）パス

`_self_review_response` で回答生成後にレビュー → `approve` / `rewrite` / `needs_tool` の分岐を検証する。

| ID | シナリオ | 送信メッセージ | 期待結果 | 確認方法 |
|---|---|---|---|---|
| QLP-019 | 通常質問 → approve | `/ask 1+1は？` | self-review で `approve` され即回答 | ログに `self-review: turn=1 action=approve` |
| QLP-020 | 根拠不足 → rewrite | `/ask 最新のGemini APIの変更点を厳密に教えて` | 根拠不足と判断されれば `rewrite` で書き直し | ログに `self-review: turn=N action=rewrite` |
| QLP-021 | 追加ツール必要 → needs_tool | `/ask この記事の要約 https://example.com/article` （URL未読み込みの場合） | `needs_tool` で追加ツール実行後に再構成 | ログに `self-review: turn=N action=needs_tool` |

### 8.7 重複ディスパッチ防止・メンション高速カレンダーパス

| ID | シナリオ | 送信メッセージ | 期待結果 | 確認方法 |
|---|---|---|---|---|
| QLP-022 | メンション高速カレンダー | `@bot 明日14時から15時に会議` | `MENTION_QUICK_CALENDAR_ENABLED=true` 時に `add_calendar_event` が LLM 判断をスキップして直接実行される | ログに `mention_quick:add_calendar_event` |
| QLP-023 | メンション高速タスク登録 | `@bot 明後日までにレポート提出タスク` | `build_quick_calendar_action` で `add_task` が検出され直接実行 | ログに `mention_quick:add_task` |

## 9. セキュリティテスト

| ID | テスト観点 | 手順 | 期待結果 | 確認方法 |
|---|---|---|---|---|
| SEC-001 | `/runcli` 承認トークン検証 | `CLI_APPROVAL_TOKEN` を不正にして承認ボタンを押す | コマンド実行が拒否される | 実行結果に認証エラー |
| SEC-002 | ホワイトリスト外アクション拒否 | `INTERNAL_ALLOWED_ACTIONS` に未登録のアクションを `/debug_action` で実行 | `unsupported_action` エラー | JSON レスポンス確認 |
| SEC-003 | Research Agent 共有トークン不一致 | `X-Research-Token` ヘッダーを不正にして `POST /v1/jobs` | `401` または `403` が返る | HTTP ステータスコード |
| SEC-004 | プロファイル user_id 分離 | ユーザーAが `/profile_set` → ユーザーBが `/profile_show` | ユーザーBにはAのプロファイルが見えない | B の表示結果に A のデータがない |
| SEC-005 | `backup_server_data` パストラバーサル | `target: "../../../etc/passwd"` で実行 | パストラバーサルを検出し拒否 | `forbidden_path` エラー |
| SEC-006 | `/runcli` タイムアウト | 承認ボタンを90秒以内に押さない | ボタンが無効化（`on_timeout`） | ボタンがグレーアウト |

## 10. 設定バリデーションテスト

| ID | テスト観点 | 手順 | 期待結果 | 確認方法 |
|---|---|---|---|---|
| CFG-001 | `BOT_GUILD_ID` 未設定 | 環境変数を削除して起動 | `BOT_GUILD_ID is required` で起動失敗 | 起動ログ |
| CFG-002 | `ALLOWED_GUILD_IDS` に不正値 | `ALLOWED_GUILD_IDS=abc,123` で起動 | `abc` は WARNING で無視、`123` のみ有効 | 起動ログに WARNING |
| CFG-003 | `DISCORD_ENABLE_MESSAGE_CONTENT_INTENT=false` | Intent 無効で起動 | `on_message` でのバックフィル・ストリーム保存がスキップ | ログに取り込みなし |
| CFG-004 | `MEMORY_RETRIEVAL_SCOPE=channel` | 環境変数設定後 `/ask` 実行 | メモリ検索がチャンネル限定 | ログに `scope=channel` |
| CFG-005 | `PERSONA_MEMORY_ENABLED=false` | 無効にして `/profile_show` 実行 | 「ペルソナ記憶は無効です。」メッセージ | ephemeral メッセージ確認 |
| CFG-006 | `MENTION_QUICK_CALENDAR_ENABLED=false` | 無効にしてメンション「明日14時に会議」 | 高速カレンダー不使用、LLM経由で処理 | ログに `mention_quick` がない |
| CFG-007 | `LOGSEARCH_DEFAULT_SCOPE=channel` | 環境変数設定後 `/logsearch Python` | デフォルトスコープが `channel` | ログに `scope: channel` |
| CFG-008 | `DISCORD_COMMAND_ALLOWLIST` 制限 | 許可リストから `logsearch` を除外して起動 | `/logsearch` がコマンド一覧に表示されない | コマンドツリー確認 |

## 11. エッジケース・境界値テスト

| ID | テスト観点 | 手順 | 期待結果 | 確認方法 |
|---|---|---|---|---|
| EDGE-001 | `/ask` 空文字列 | `/ask ""` | Bot が安全にエラーまたは空応答を返す | Bot が落ちない |
| EDGE-002 | DM（ギルド外）で `/ask` | DM チャンネルで `/ask` 実行 | 「このサーバーでは利用できません」（`guild_id is None`） | ephemeral メッセージ |
| EDGE-003 | 応答2000文字ぎりぎりの分割 | 1900文字前後の応答が出る質問 | `MAX_DISCORD_MESSAGE_LEN` で正しくチャンク分割 | 2メッセージに分割されるか確認 |
| EDGE-004 | 15,000文字超のファイル添付フォールバック | 非常に長い回答が生成される質問 | `MAX_TOTAL_INLINE` 超過時にテキストファイル添付 | `ask_response.txt` が添付される |
| EDGE-005 | 同一ドメイン重複除外 | 検索で同ドメインの複数結果が出る質問 | 重複URLが除外される | 参考URL欄の確認 |
| EDGE-006 | `add_calendar_event` で `end_time < start_time` | `end_time` が `start_time` より前の値 | エラーまたは翌日跨ぎ補正 | 結果確認 |
| EDGE-007 | `add_task` にタイトル空 | `/debug_action add_task payload_json: {"title":""}` | `missing_title` エラー | JSON レスポンス |
| EDGE-008 | `/profile_set` key 48文字超 | 49文字のキーで `/profile_set` | 「key は48文字以内で指定してください」 | ephemeral メッセージ |
| EDGE-009 | `/profile_set` value 500文字超 | 501文字の値で `/profile_set` | 「value は500文字以内で指定してください」 | ephemeral メッセージ |
| EDGE-010 | `/logsearch` 空キーワード | `/logsearch ""`| 「キーワードが空です」 | ephemeral メッセージ |
| EDGE-011 | `/runcli` 空コマンド | `/runcli ""` | 「コマンドが空です」 | ephemeral メッセージ |
| EDGE-012 | メンション + 高速カレンダーで全角数字 | `@bot 明日１４時から１５時に打ち合わせ` | 全角→半角変換されてカレンダー登録成功 | カレンダーイベント確認 |
