# Change Log

このファイルを本プロジェクトの正式な変更履歴として運用します。

## 2026-04-23

### 機能廃止（音声対話・音楽連携）

- Bluetooth の HSP/HFP プロファイル使用時の音質劣化問題を考慮し、Voice STT 関連機能を完全に削除した
  - `src/voice_stt_agent/` ディレクトリおよび関連コードの削除
  - `docker/Dockerfile.voice_stt` の削除
  - `src/tools/music_tools.py` （Spotify/音楽連携ツール）の削除
  - `main-agent` から音声受信・転送・デコードロジック（`DiscordAudioBridgeSink`, `VoiceChunkForwarder`）を削除
  - `/vc_join`, `/vc_leave`, `/vc_status` コマンドの削除
  - `docker-compose.yml` から `voice-stt-agent` サービスおよび `voice` プロファイルを削除
  - `.env` / `.env.example` から音声関連の設定項目を削除
  - `requirements.txt` から `discord-ext-voice-recv`, `PyNaCl` を削除
- **不要なローカルサービスの完全撤去**:
  - `docker-compose.yml` から `ollama` サービスを削除した
  - コード精査により、Gemini 503 時のフォールバック動作は Gemini API (クラウド) 上の Gemma モデルを直接呼び出しており、ローカルの Ollama サーバーを必要としないことを確認した
  - これにより、N100 サーバーの貴重なメモリおよび CPU リソースを大幅に解放し、システム全体の安定性を向上させた

### アーキテクチャ改善・バグ修正

- **会話文脈の保持（フォローアップ解決）の強化**:
  - `つまり`, `これ`, `それって`, `要するに` などの接続詞・代名詞をフォローアップマーカーとして新規登録し、これらを含む質問で前後の文脈が無視される問題を改善した
  - 一般知識クエリ判定（History Context スキップ）の条件を緩和し、30文字以下の短い質問（例: 「vmbr0とは？」）では一般知識っぽく見えても履歴検索を実行するように変更した
- **Gemma プロンプトリーク検知の強化**:
  - Gemma が内部思考過程（Role, Constraints 等の見出し）を誤って出力した際の検知パターンを拡充し、複数の見出しが並んでいる場合に自動的に安全なエラーメッセージへ差し替えるように改善した

## 2026-04-20

### バグ修正・安定性向上（音声文字起こしパイプライン）

- Whisper のハルシネーション（「ご視聴ありがとうございました」「チャンネル登録といいねをお願いいたします」等）が実際の音声の代わりに出力される致命的バグを修正した
  - **根本原因1: ダウンサンプリングの欠如**: Discord Opus デコーダの出力（48kHz ステレオ）をそのまま WAV 化して Whisper に送信していたのを、`audioop.tomono()` + `audioop.ratecv()` で **16kHz モノラルに変換**してから送信するようにした。Whisper が内部的に期待する 16kHz モノラルに事前変換することで、認識精度を大幅に向上させた
  - **根本原因2: VAD 完全無効化の弊害**: 前回修正で `vad_filter = False` に固定したことで、無音・ノイズだけのチャンクも Whisper に渡されてハルシネーションが発生していた。VAD を **再有効化**し、`threshold=0.3`（既定の0.5より緩い）で微小な音声も通すように設定した
  - **根本原因3: transcribe パラメータ未最適化**: `condition_on_previous_text=False`（幻覚連鎖防止）、`compression_ratio_threshold=2.0`（反復パターン除去）、`log_prob_threshold=-0.8`（低品質セグメント除去）、`initial_prompt` による文脈ガイドを追加した
- Whisper の既知ハルシネーションパターン（「ご視聴ありがとう」「チャンネル登録」「お疲れ様でした」等13パターン）をフィルタリングする `_is_whisper_hallucination()` を実装した。30文字以下の短いテキストのみフィルタし、実際の講義内容を誤ってブロックしないようにした

## 2026-04-19

### バグ修正・安定性向上（音声パイプライン）

- Discord Voiceからの音声ストリーム受信時に発生していた `OpusError: corrupted stream` を解消するため、`discord.ext.voice_recv` の自動デコードを避け、`wants_opus=True` と手動の Opus デコードへ実装を切り替えた
- `DiscordAudioBridgeSink` に無音フラッシュ（Silence Flush）のバックグラウンドタスクを追加し、0.8秒の無音を検知した際にバッファが通常チャンクサイズに到達しなくても強制的に音声認識へ送信するようにした
- `VoiceChunkForwarder` タスクが正しいイベントループに紐づかず停止してしまう問題を修正し、起動時にキューのコンシューマが走るように改善した
- `voice-stt-agent` 側（Faster-Whisper）のVAD（無音除去フィルタ）が厳しすぎて、ノイズ混じりの微小な音声チャンクから必要な声を削り落として幻覚（「お疲れ様です」等）を引き起こす問題に対処し、コード上で強制的に `vad_filter = False` へハードコード固定し、確実にすべて音声を文字起こしに回すよう修正した

### パフォーマンス向上

- `main-agent` の `/vc_join` コマンド実行時に、バックグラウンド処理で `voice-stt-agent`（Ollama / Gemma）へAPIを投げ、意図抽出用LLMモデル（`gemma4`）をメモリに事前展開（Preload）する機能を実装した。これにより、初回音声入力時の意図抽出が遅延する問題（Cold Start問題）を劇的に改善した

### アーキテクチャ改善（Research Agent モデル・認証分離）

- Research Agent 内の「管理エージェント」と「調べるエージェント」のモデル・認証方式を完全に分離した
  - **管理エージェント**: APIキー認証 (`google-generativeai`) + `gemini-3.1-flash-lite-preview` / フォールバック `gemma-4-31b-it`
  - **調べるエージェント**: Gemini CLI OAuth認証 (`google-genai`) + `gemini-3.1-pro` / フォールバック `gemini-3.1-flash`
- `google-genai` (新SDK) を依存に追加し、`google-generativeai` (旧SDK) と共存させる構成にした
- Gemini CLI の `$HOME/.gemini/oauth_creds.json` から OAuth トークンを読み込み、`google.oauth2.credentials.Credentials` に変換するアダプタ `_load_gemini_cli_credentials()` を新規実装した
- CLI クライアントが利用不可（認証ファイル未設定、パッケージ未インストール等）の場合、管理エージェントの APIキー経由に安全にフォールバックする設計とした
- CLI モデルのメイン・フォールバック双方が失敗した場合の最終フォールバックとして管理エージェント APIキー経由の呼び出しを追加した
- `_call_gemini_cli()` メソッドを新設し、`answer()` メソッドの `model_call` を CLI 認証経由に変更した
- ログラベルを `research-manager` → `research-investigator` に変更し、管理エージェントとの区別を明確にした

### 設定変更

- `.env` / `.env.example` に以下の環境変数を追加:
  - `RESEARCH_AGENT_CLI_MODEL`: 調べるエージェントのメインモデル（既定: `gemini-3.1-pro`）
  - `RESEARCH_AGENT_CLI_FALLBACK_MODEL`: 調べるエージェントのフォールバックモデル（既定: `gemini-3.1-flash`）
- `RESEARCH_AGENT_GEMINI_MODEL` を `gemini-3.1-pro` から `gemini-3.1-flash-lite-preview` に戻し、管理エージェント専用として明確化した
- `.env` 31行目の重複していた `RESEARCH_AGENT_GEMINI_503_FALLBACK_MODEL` を削除した
- `requirements.txt` に `google-genai>=1.0.0` と `google-auth>=2.0.0` を追加した

## 2026-04-16

### バグ修正

- Gemma フォールバック時に「内部整形で問題が発生したため、この依頼は実行結果を確定できませんでした」エラーが誤発生する問題を修正した
  - 原因: Gemma (gemma-4-31b-it) が chain-of-thought（思考プロセス）テキストを応答に含め、`_looks_like_internal_prompt_leak` が `The user wants to` / `[Tool Results]` 等のマーカーを誤検知していた
  - `_strip_gemma_thinking()` メソッドを新規追加し、Gemma の思考行（`*   ` で始まる分析テキスト）を除去して最終回答のみを返すようにした
  - `_compose_final_response()` および `_generate_with_tools()` のレスポンスパス（2箇所）で、prompt-leak チェック前に stripping を適用した

### 設定変更

- RAG（会話履歴コンテキストのプロンプト注入）を再有効化した（`PROMPT_INCLUDE_HISTORY_CONTEXT=true`）
- ペルソナ記憶のプロンプト注入を再有効化した（`PROMPT_INCLUDE_PERSONA_CONTEXT=true`）
- メモリ検索スコープを `guild` → `channel` に変更し、無関係な別チャンネルメッセージのノイズ混入を低減した
- メモリ取得件数を `MEMORY_TOP_K=8` → `4` に変更し、プロンプトサイズとノイズを削減した
- 全エージェント（Main, Research, Voice STT）のログおよび監査ログ（ai_exchange.log, research_audit, runcli_audit等）のタイムスタンプを UTC から JST (日本標準時) に変更した
  - `timedelta(hours=9)` を明示的に使用して、ログの可視性とデバッグの効率を向上させた

## 2026-04-01


## 2026-04-02

## 2026-04-03

## 2026-04-04

### 仕様更新

- Research Agent の責務を「Gemini/Gemma の選択・校閲・返却可否判定・原文アーティファクト保存」に縮小し、深掘りの tool loop は Gemini API / Gemma 4 側へ移した
- Gemini CLI 依存をやめ、Research Agent は Python native の Gemini API 呼び出しを標準経路にした

### 実装

- `src/tools/research_loop.py` を追加し、Gemini と Gemma が同じ JSON tool loop 契約で `web_search` / `read_url_markdown` / `source_deep_dive` を回せる共通実行器を導入した
- `src/research_agent/core/orchestrator.py` を共通 loop 接続へ切り替え、Gemini 側の生トランスクリプトを保持できるようにした
- `src/gemma_worker/worker_server.py` を tool loop 実行モードへ変更し、Gemma の推論結果に raw transcript と decision log を含めた
- `src/research_agent/research_agent_server.py` を裁定/検査中心に簡素化し、ジョブ完了時に原文アーティファクトを保存して `artifact_path` として返せるようにした
- `src/main_agent/main.py` の研究完了通知で、保存済みの原文アーティファクトがあれば優先的に添付するようにした

### 仕様更新

- 現在実装準拠の責務/経路図として `docs/ROUTE_DIAGRAM_CURRENT.md` を追加し、Main/Research/管理オーケストレータ/Gemini CLI/Gemma Worker の分岐とログ追跡ポイントを明文化した

### 実装

- Ollamaモデルを `gemma3:4b` から `gemma4:e2b` へ切り替え、`gemma3:4b` / `gemma4:e4b` を削除してN100メモリ制約下で安定動作する構成へ調整した
- Heretic派生へ切り替えやすいように、`OLLAMA_MODEL` の差し替え候補を `.env` と `.env.example` にコメントで残した
- Gemma 4 E4B運用向けに `GEMMA_WORKER_HTTP_TIMEOUT_SEC=240` と `GEMMA_WORKER_NUM_PREDICT=128` を既定化し、長時間推論のタイムアウトを緩和した
- `gemma-worker` の `/v1/research/analyze` に短縮リトライ経路を追加し、一次推論失敗時でもGemma要約を返しやすくした
- `docker-compose.yml` に `ollama` サービスを追加し、`data/ollama` ボリューム永続化・healthcheck・profile(`gemma`)付きで管理できるようにした
- `gemma-worker` を profile(`gemma`)配下へ移し、`ollama` のヘルス完了後に起動する `depends_on` 条件を追加した
- `OLLAMA_BASE_URL` の既定を `http://ollama:11434` へ更新し、compose内サービス参照を標準化した
- `gemma-worker` の research analyze で深掘り再取得を既定OFF（`GEMMA_WORKER_RESEARCH_USE_DEEP_DIVE=false`）にし、Ollamaタイムアウト時のフォールバック頻度を低減した
- `GEMMA_WORKER_NUM_PREDICT` / `GEMMA_WORKER_TEMPERATURE` を追加し、N100向けにGemma生成長を抑えて応答安定性を改善した
- `src/gemma_worker/worker_server.py` を追加し、`/v1/logsearch/rerank` と `/v1/research/analyze` を提供するGemma Workerを実装した
- `docker/Dockerfile.gemma` を追加し、Gemma Workerを独立コンテナとして起動できるようにした
- `docker-compose.yml` に `gemma-worker` サービスを追加し、`main-agent` / `research-agent` から参照できる構成へ更新した
- Research Agentに4段目（Gemma 4）委譲の実装を追加し、時間指定の明示 + 深掘り意図語 + しきい値（既定10分）を満たす場合のみGemmaステージを発火するようにした
- `dispatch_research_job` から `time_specified` フラグをResearch Agentへ渡し、時間指定有無に基づいた発火判定ができるようにした
- Gemma呼び出し失敗時は既存ルート（Gemini CLI / Orchestrator / DeepDive）へフォールバックするようにし、ジョブ失敗率を上げない実装にした
- `/logsearch` に Gemma補助リランキング（任意有効）を追加し、上位候補の再並び替えをローカル推論APIへ委譲できるようにした（失敗時は既存スコアへフォールバック）
- `.env.example` に `LOGSEARCH_GEMMA_*` 設定（有効化、エンドポイント、タイムアウト、候補倍率）を追加した
- `.env.example` に `RESEARCH_AGENT_GEMMA_*` 設定（有効化、エンドポイント、タイムアウト、発火しきい値、例外起動可否）を追加した
- `.env.example` に `GEMMA_WORKER_*` と `OLLAMA_*` 設定を追加し、別コンテナ運用時の接続先を明示した

- `/ask` に URL比較時の Reader直行ルートを追加し、比較質問で Research Agent へ過剰委譲される経路を抑制した
- メンション判定の既定を「先頭一致必須」から「文中メンション許容」へ変更し、`今日は @bot ...` 形式でも反応するようにした（`MENTION_REQUIRE_PREFIX` で従来挙動に戻し可能）
- 一般知識クエリ判定を追加し、フォローアップでない質問では履歴注入を抑制して無関係メモリ混入を低減した
- 方向付きメモリ境界の設定読み込みを堅牢化し、`PERSONAL_GUILD_ID` の不正値で起動失敗しにくいようにした

### 仕様更新

- `docs/DESIGN.md` に N100導入時の標準分離（`main-agent` / `research-agent` / `gemma-worker`）を追記した
- `docs/DESIGN.md` の Discord過去ログ検索を「将来拡張」から「拡張実装対象」へ更新し、Gemma rerank を初期実装方針として明文化した
- 提案方針として「提案1/提案2を採用、提案3（全メッセージ常時ゲート）は既定不採用」を仕様へ反映した
- `docs/DESIGN.md` に多段委譲ポリシーを追記し、最終回答品質責任をResearch管理エージェント（Gemini API）に置く方針を明文化した
- 4段目（Gemma 4）の起動基準を追加し、「時間指定のじっくり調査」を主条件、深掘り語・時間しきい値（推奨10分）・高精度タスク例外を仕様化した
- 4段目出力フォーマット（根拠URL、要点、反証・異説、不確実点）を標準化し、Gemmaは網羅収集/抽出、Geminiは最終統合を担う責務分離を追記した

- `docs/DESIGN.md` のロードマップを現行実装に合わせて更新し、以下を明文化した
	- Discord過去ログ検索（`/logsearch`）は実装済み
	- Eternal Explorer とカスタムドキュメントRAGは当面非対象
	- runcli関連強化は後回し
	- システム権限管理は方向付きメモリ境界で最小実装済み（個人→身内参照可、逆方向/身内間不可）
- `docs/DESIGN.md` の `.env` サンプルに `add_task/update_task/delete_task` と一括削除/一括更新アクションを反映した

### テスト項目書更新

- `docs/TEST_PLAN.md` にエラーハンドリング・耐障害性テスト（§7: ERR-001〜ERR-010）を追加した

### 再テスト・不具合管理更新

- `docs/TEST_RESULTS.md` で FAIL 再検証の 1st batch を反映（UT-061, CT-010, CT-026, MT-014/016/026/033, QLP-005 を更新）
- `src/tools/action_tools.py` の `get_calendar_events` ローカル分岐で発生していた `NameError(storage_path)` を修正
- `docs/MANUAL_TEST_ITEMS.md` に BUG-004 / BUG-010 の実機検証ランブックとクローズ条件を追加
- `docs/BUG_TRACKER.md` に BUG-004 / BUG-010 の実機クローズ条件を追記
- 実機ログ反映: CT-004 は `PASS（実機）` へ更新、CT-020 は `PASS（実機）` として BUG-010 を Fixed 化
- CT-006 は polling 表示確認手順が未確定のため後回しとして継続管理
- Discord質問ロジックパス網羅テスト（§8: QLP-001〜QLP-023）を追加した。Research Controls 注入、Recent Conversation 文脈組立、フォローアップ解決・指示語注入、曖昧クエリ検出・拒否、強制ディスパッチ、Self-Review、メンション高速カレンダー/タスクの全パスをカバー
- セキュリティテスト（§9: SEC-001〜SEC-006）、設定バリデーションテスト（§10: CFG-001〜CFG-008）、エッジケース・境界値テスト（§11: EDGE-001〜EDGE-012）を追加した
- テスト実施手順（§5.1）に新セクション（§7〜§11）の実施順序を追記した
- `docs/TEST_RESULTS.md` を新規作成。全192件のテスト結果記録ファイルとして、ID・判定・実行ルート・使用ツール/エージェント・観測結果を記録する構造を定義した


### 仕様更新

- Research Agent の `mode=fallback` を「Gemini CLIを使わず、管理AI（Gemini API Orchestrator）を優先するモード」として明確化した
- `mode=auto` は「Gemini CLI先行 + 必要時に管理AIを追加」の方針を明確化し、管理AI失敗時のみ `source_deep_dive` へフォールバックする仕様に整理した

### 実装

- `src/research_agent/research_agent_server.py` の実行フローを更新し、`mode=fallback` で Orchestrator を優先実行するようにした
- Research Job 完了時に `decision_log` を SQLite へ保存して API レスポンスで返せるようにした
- `src/tools/deep_dive_tools.py` と `src/main_agent/tools/deep_dive_tools.py` にクエリ重複除去を追加し、同一テーマの重複検索を抑制した
- `RESEARCH_AGENT_GEMINI_MODEL` を導入し、Gemini CLI 起動時に `--model` を明示指定することで Manual 設定依存を回避した
- ツール実装を `src/tools/` へ完全集約し、`src/main_agent/tools/` の重複実装を削除した
- Dockerfile を `docker/` 配下へ移設し、`docker-compose.yml` の build 参照先を更新した

### 仕様更新

- Main Agentの実装ディレクトリ表記を `src/main_agent/` へ更新した（旧 `src/discord_ai_agent/` は互換shimとして残置）
- Research Agentを段階実装対象へ更新し、別コンテナ + 軽量HTTP + 共有トークンの最小通信方式を仕様に追記した
- Gemini CLI配置方針を追記し、Research Agentコンテナ同梱を既定推奨、ホスト実行を代替案として明文化した
- n8n中継方式を非推奨化し、外部アクションをBotコード内で直接実行する方針へ更新した
- `/n8n_action` 中心の運用方針を `/action` 中心へ更新し、`/n8n_action` は互換コマンドとして扱う方針へ変更した
- `.env` 仕様を `N8N_*` 系から `INTERNAL_*` 系へ移行し、認証誘導URL（`GITHUB_AUTH_URL` / `SMTP_AUTH_URL`）を追加した
- フェーズ分割前提を廃止し、全機能を設計対象とする統一方針へ更新した
- 本運用必須4要件（キュー制御・チェックポイント・JSON強制・疑似成功禁止）を追加した
- 未実装機能の扱いを `not_implemented_action` エラー返却へ統一する方針を追加した
- `.env` 仕様へ `MAX_CONCURRENT_HEAVY_TASKS` / `HEAVY_TASK_TIMEOUT_SEC` / `CHECKPOINT_DB_PATH` を追加した
- `docs/DESIGN.md` から Phase 前提の記述を外し、継続実装前提の文書へ再編
- Tool章を「4本柱」から「6本柱」へ更新し、将来実装枠として「Discord過去ログ検索ツール」「n8n Webhookツール」を追加
- データベース設計章へ将来拡張枠として「カスタムドキュメントRAG（PDF/テキスト）」「SQLite（権限/承認待ち管理）」を追加
- 0.3 の非実装項目へ、n8n/Discord過去ログ検索/カスタムドキュメント/SQLite を明記
- メモリ受け入れ条件を現行実装に合わせ、検索スコープ制御可能な要件へ更新
- ロードマップ章をフェーズ分割から継続実装ロードマップへ更新
- Discord運用仕様へ `/logsearch`（scope=channel/guild、明示キーワード検索）を追記
- `.env` 仕様へ `LOGSEARCH_DEFAULT_SCOPE` を追記
- 第二の自分化要件として、長期ペルソナ記憶（性格/好み/長期目標/定常タスク）を仕様へ追加
- メモリ要件へ、同意ポリシー（opt-in）・削除権・説明可能性・`user_id`分離要件を追記
- Discord運用仕様へ `/profile_show` `/profile_set` `/profile_forget` の任意追加方針を追記
- `.env` 仕様へ `PERSONA_MEMORY_*` 系設定（有効化/確認フラグ/件数上限/コレクション名/証跡件数）を追記
- `/logsearch` の結果表示に簡易スコア（一致度/新しさ）を追加できる仕様を追記
- `/runcli_audit` のevent種別フィルタ仕様を追記
- `/n8n_action` のaction別必須キー検証仕様を追記
- n8n標準インターフェースとして `execute_n8n_workflow`（`action` + `parameters`）のJSON仕様を追記
- n8nアクション定義を `add_calendar_event` / `get_calendar_events` / `send_email` へ整理し、必須キーを明記
- Gemini Web会話エクスポート取り込み方針（対応形式: json/csv、mpegは文字起こし前提）を追記
- 運用用 `.env` に許可ギルド3件（個人1 + 身内2）と n8n関連設定を反映
- `docker-compose.yml` に n8nサービス（ローカルバインド/永続化/Basic Auth）を追加
- `README.md` に n8n構築手順、Webhook保護、Google Calendar OAuth設定手順を追加
- `.env.example` を n8nセルフホスト運用の設定項目に合わせて更新
- 本番環境でもn8n設定思想を維持しつつ、本番専用シークレット/URLへ切替する運用方針を明文化
- n8n初心者向けに、画面クリック手順を含む本番導入ガイド `docs/N8N_DEPLOYMENT_BEGINNER_GUIDE.md` を追加
- n8n初心者向けガイドに「最短手順」を追記し、入力キーとクリック順のみで実施できる形式へ簡略化
- n8n作業の要否を先に判断できる分岐（必要/不要）をガイドへ追加
- 開発用/本番用のコピペ可能な `.env` 完成形サンプルをガイドへ追加
- n8n運用ガイドに、GUIを使わない完全CUI手順（list/publish/restart/export/curl）を追記
- 方向付きメモリ境界（`DIRECTIONAL_MEMORY_ENABLED` / `PERSONAL_GUILD_ID` / `FAMILY_GUILD_IDS`）の仕様を追加
- `/ask` を入口にAIが自律ツール選択する運用方針へ合わせ、ask限定モード表現を撤回
- n8nアクション定義を拡張（`add_notion_memo` / `append_sheet_row` / `create_github_issue` / `send_line_notification` / `backup_server_data`）
- メッセージ連携要件を LINE 通知から Slack メッセージ取得へ切替し、`get_slack_messages`（必須: `channel_id`, `limit`）を反映
- Slack取得にはBot/Appトークンが実質必須のため、運用方針に合わせて `get_slack_messages` を見送り `send_email` に戻した

### 実装

- 互換shimを整理し、`src/discord_ai_agent/` と `src/main_agent/research_agent_server.py` を削除して、Research実体を `src/research_agent/research_agent_server.py` に一本化した
- `deepdive` のResearch投入時に `job_id` / `topic` / `source` を `research_audit.jsonl` へ監査記録するようにした
- Research完了通知を改善し、長文レポートは要約メッセージ + `research_report.txt` 添付へ切替えるようにした
- docker compose のMainサービス名/コンテナ名を `main-agent` へ変更した
- 実装パッケージを `src/main_agent/` へ移行し、`src/discord_ai_agent/` は後方互換用の薄いshimへ変更した
- `research-agent` サービスを `docker-compose.yml` に追加し、Main/Researchの別コンテナ分離を実装した
- `src/discord_ai_agent/research_agent_server.py` を追加し、`POST /v1/jobs` / `GET /v1/jobs/{job_id}` とSQLiteジョブ状態管理を実装した
- `dispatch_research_job` ツールを追加し、Main AgentからResearch Agentへジョブ投入・ポーリング取得できるようにした
- `get_research_job_status` ツールを追加し、Main AgentからResearchジョブ状態を参照できるようにした
- `deepdive` コマンドに Research Agent 経由モード（`DEEPDIVE_USE_RESEARCH_AGENT=true`）を追加した
- `deepdive` のResearch経由時を非同期投入に変更し、完了/失敗を同一チャンネルへ自動通知するバックグラウンドポーラーを実装した
- Main起動時に `research_job` チェックポイント（queued）から通知ポーラーを再開する処理を追加した
- `.env.example` に `RESEARCH_AGENT_*` / `DEEPDIVE_USE_RESEARCH_AGENT` 設定を追加した
- `/n8n_action` 互換コマンドを削除し、`/action` のみを正式コマンドとして運用するようにした
- `/action` を `debug_action`（デバッグ専用）へ移行し、通常運用は `/ask` 中心とする方針へ更新した
- `/auth_status` コマンドを追加し、外部連携の認証状態と導線URLをDiscord上で確認できるようにした
- 起動時にグローバル/ギルドのコマンド再同期前に clear を実行し、旧コマンドの残留を自動クリーンアップするようにした
- `backup_server_data` の内部アクション実装を追加し、許可ルート配下のみを `.tar.gz` へバックアップできるようにした
- `append_sheet_row` の内部アクション実装を追加し、`SHEET_STORAGE_DIR` 配下のCSVへ行追加できるようにした
- `add_notion_memo` の内部アクション実装を追加し、`NOTION_MEMO_STORAGE_PATH` へJSON Lines形式で記録できるようにした
- `execute_internal_action` ツールを追加し、Webhook中継なしで action をコード内実行する方式へ移行した
- メンション高速カレンダールーターで和文日付（例: `2026年4月7日`）を解釈できるようにし、定型入力の取りこぼしを削減した
- メンション高速カレンダールーターで終日表現（`終日` / `全日` / `一日中`）を `00:00-23:59` として処理できるようにした
- メンション高速カレンダールーターで「内容/日時」形式の箇条書き文（追加キーワードなし）も予定追加意図として判定できるようにした
- `add_calendar_event` / `get_calendar_events` の日時パーサーを強化し、ISO8601に加えて和文・簡易日付時刻フォーマットを受理するよう改善した
- 内部アクション日時でタイムゾーン未指定時は `TZ`（既定: `Asia/Tokyo`）を補完するようにし、時刻形式エラーの発生率を低減した
- `add_calendar_event` に終日イベントのネイティブ登録（`all_day=true`, `date`, `end_date`）を追加し、00:00-23:59擬似登録に依存しない運用へ改善した
- メンション高速ルーターで `00:00-23:59` 指定を終日として自動解釈する補正を追加した
- オーケストレーター方針文を更新し、終日指定時に時刻確認を要求せず `all_day + date` 形式で実行するよう明示した
- `execute_internal_action` に action別名の正規化を追加し、`calendar_add_event` / `calendar_get_events` を `add_calendar_event` / `get_calendar_events` へ自動変換できるようにした
- メンション高速ルーターで `4月5日` のような和文月日入力を解釈できるようにし、終日登録の取りこぼしを低減した
- オーケストレーター方針文を更新し、入力が明確な予定追加は確認質問を省略して実行するルールを強化した
- `add_calendar_event` のpayload正規化を追加し、`summary` / `event` / `name` などの同義キーから `title` を補完できるようにした
- `docs/DESIGN.md` を更新し、Research Agent/Eternal Explorer の別コンテナ分離推奨方針を追記した
- `docs/DESIGN.md` と `.env.example` の `INTERNAL_ACTION_REQUIRED_FIELDS` を更新し、`add_calendar_event` は `title` を共通必須として timed/all-day の二方式を許可した
- `create_github_issue` と `send_email` の内部アクション実装を追加し、認証未設定時は `auth_required` と `auth_url` を返すようにした
- `docker-compose.yml` から n8n サービスを外し、Bot単体 + runtime永続ボリューム構成へ変更した
- `docs/N8N_DEPLOYMENT_BEGINNER_GUIDE.md` を非推奨ガイドへ更新し、現行運用導線を `README.md` / `docs/DESIGN.md` へ統一した
- オーケストレーターに重い処理の同時実行制限（`asyncio.Semaphore`）を追加し、`/ask` と重いツール実行をキュー制御下へ移行した
- オーケストレーターへ SQLite ベースのチェックポイント保存API（save/load/list）を追加した
- Gemini意思決定系の呼び出しで `response_mime_type=application/json` を指定し、JSONパース安定性を強化した
- n8n未実装アクションの `stub-success` 応答を廃止し、`not_implemented_action` + HTTP 501 を返すよう変更した
- `create_github_issue` アクションを n8n 側で stub 応答から実API呼び出しへ更新し、`GITHUB_TOKEN` 未設定時は 503 を返す安全分岐を追加した
- n8n の `create_github_issue` で GitHub API 応答を判定し、成功時（201）と失敗時（4xx/5xx）でJSONレスポンスを分離した
- `trigger_n8n_webhook` に再試行設定（`N8N_RETRY_COUNT` / `N8N_RETRY_BACKOFF_SEC`）を追加し、HTTPエラー時のデバッグ情報を強化した
- `/logsearch` コマンドを追加し、Discord上で過去ログ候補を明示検索できるようにした
- `/runcli_audit` コマンドを追加し、runcli監査ログ（JSON Lines）の末尾イベントをephemeral表示できるようにした
- `.env` 仕様へ `RUNCLI_AUDIT_TAIL_DEFAULT` を追加した
- `trigger_n8n_webhook` ツールを追加し、許可済みactionのみn8n webhookへJSON POSTできるようにした
- `/n8n_action` コマンドを追加し、Discordからn8n actionをephemeralで実行できるようにした
- `.env` 仕様へ `N8N_WEBHOOK_BASE_URL` / `N8N_ALLOWED_ACTIONS` / `N8N_WEBHOOK_TOKEN` / `N8N_TIMEOUT_SEC` を追加した
- `/logsearch` に一致度/新しさスコア表示を追加した（設定で無効化可能）
- `/runcli_audit` にevent種別フィルタ（all/requested/approved/rejected/executed/...）を追加した
- `trigger_n8n_webhook` に `N8N_ACTION_REQUIRED_FIELDS` ベースの必須キー検証を追加した
- `ChannelMemoryStore` にユーザー単位ペルソナ記憶のCRUD（set/get/forget）を追加した
- `/profile_show` `/profile_set` `/profile_forget` を追加し、ユーザープロファイルをDiscordから管理可能にした
- 回答生成時に `PERSONA_MEMORY_INCLUDE_IN_PROMPT=true` ならユーザープロファイルをシステム文脈へ注入するようにした
- `.env.example` に `PERSONA_MEMORY_*` 設定群を追加した
- 方向付きメモリ境界設定（`DIRECTIONAL_MEMORY_ENABLED` / `PERSONAL_GUILD_ID` / `FAMILY_GUILD_IDS`）を追加し、個人サーバーからのみ身内サーバー記憶を参照できるポリシーを実装した
- n8nの `execute_n8n_workflow` をCUIで再現可能にするテンプレート `n8n/workflows/execute_n8n_workflow.json` を追加した
- `ASK_ONLY_MODE` による補助コマンド遮断を撤回し、`/ask` 入口からの自律ツール利用モデルへ戻した
- n8n webhook呼び出しを `action + parameters` 形式の単一エンドポイント（`execute_n8n_workflow`）へ統一した

## 2026-03-31

### 仕様策定・明確化

- `add_calendar_event` / `get_calendar_events` の内部アクション実装を追加し、`CALENDAR_EVENTS_STORAGE_PATH` へJSON Lines形式で記録・期間検索できるようにした
- initial_profileの欠損時挙動、サイズ上限、推奨テンプレートを追加
- Web検索の件数、タイムアウト、再試行、出力フォーマットを固定
- ChromaDBの分離キー、命名規則、保存スキーマ、検索件数を固定
- /ask の最小仕様、ギルド制御、長文応答分割、添付フォールバックを確定
- ログ方針と .env 推奨設定を確定
- Google GroundingはPhase 1では既定不採用、Phase 2候補に整理
- 開発停滞回避のため、フェーズ制限を緩和する「実装優先モード」を追加

### 実装

- Discord BotのPhase 1実装を追加
- Main Agent、Orchestrator、Memory、Search Toolを実装
- `ask_response.txt` 添付方針を実装
- 同一ドメイン重複除外の検索整形を実装
- Gemini連携をGoogle公式SDK直呼び出しへ移行（LangChain依存を削減）
- Gemini tool callingの`thought_signature`問題を回避するため、Orchestrator側のツール判定・実行方式へ変更
- `search_tools.py` からLangChain `@tool` デコレータ依存を除去し、独立関数として再実装
- Orchestratorに自律検索計画を追加（最大3クエリ、最新性判定、結論先出しポリシー）
- 天気・ニュース・価格系質問で補助クエリを自動生成し、回答の具体性を向上
- 未使用の `src/discord_ai_agent/core/orchestrator_v2.py` を削除し、実行対象を `orchestrator.py` に一本化
- Orchestratorの検索計画を汎用化し、LLMによる自律クエリ生成（JSON計画）を追加
- 検索計画失敗時はヒューリスティックへフォールバックする二段構えに改善
- ツールレジストリを導入し、`web_search` / `read_url_markdown` / `source_deep_dive` / `run_local_cli` を実装
- オーケストレーターを汎用の自律ツールループ方式へ更新（各ターンで tool または respond を選択）
- CLIツールにHitL相当の安全策（承認トークン + 許可コマンド制限）を追加
- DuckDuckGoレート制限対策として、検索キャッシュ（TTL）とクールダウン制御を追加
- 深掘りツールでレート制限検知時に残りクエリを早期停止する制御を追加
- ツールレジストリに引数バリデーション/正規化を追加し、LLMの誤ったツール引数を実行前に遮断
- オーケストレーターに自己評価ループ（approve/rewrite/needs_tool）を追加し、回答前に品質検査を実施
- エージェント意思決定ログ（turn/action/tool/reason）とツール結果サマリログを追加
- `/ask` 実行時に直近チャンネル履歴をバックフィルし、Bot導入前メッセージをメモリ化する処理を追加
- メモリ保存を `add` から `upsert` に変更し、Discord `message_id` をキーに重複保存を抑制
- メモリ検索をハイブリッド化（語彙重なり優先 + 新しさフォールバック）し、想起精度を改善
- 履歴取り込みを `/ask` 同期フェッチ方式から、起動時バックフィル + `on_message` 常時収集方式へ変更
- 取り込みカーソルを `data/chromadb/memory_ingest_cursor.json` に永続化し、再起動時は差分のみ履歴同期するよう改善
- Message Content Intentが未許可環境でクラッシュしないよう、`DISCORD_ENABLE_MESSAGE_CONTENT_INTENT` を追加（既定false）
- Intent無効時は全量履歴取り込みをスキップし、起動を優先するフェイルセーフを追加
- メモリ保存をチャンネル単位に加えてギルド全体インデックスへも同時保存し、サーバ全体の想起に対応
- 回答時メモリ参照を既定で `MEMORY_RETRIEVAL_SCOPE=guild` に変更し、別チャンネル過去会話も参照可能に改善
- 起動時バックフィル対象をテキストチャンネルに加えてアクティブスレッドへ拡張
- `MEMORY_BOOTSTRAP_FORCE_REINDEX` を追加し、既存履歴の再インデックスを差分カーソルを無視して実行可能にした
- メモリ検索を `collection.get(limit=...)` 依存から `collection.query(...)` ベースへ変更し、古い履歴の取りこぼしを低減
- メモリ取得ログにヒット件数と参照チャンネル一覧を追加し、クロスチャンネル参照可否を運用で検証しやすく改善
- メモリ保存時にDiscordメッセージの作成時刻を保持するよう変更し、バックフィル後の日時ズレを軽減
- 起動時バックフィル対象をアーカイブ済みスレッドまで拡張し、長期間の会話取りこぼしを低減
- 1回限りの全再インデックス運用（`MEMORY_BOOTSTRAP_FORCE_REINDEX=true`）を実施し、ギルド履歴の再同期を確認
- 回答末尾に参照メモリ出典を付与できる `MEMORY_RESPONSE_INCLUDE_EVIDENCE` / `MEMORY_RESPONSE_EVIDENCE_ITEMS` を追加
- 「過去ログ参照権限がない」系の既知テンプレート回答をメモリ再ランク時に減点し、誤った再提示を抑制
- 参照メモリ出典の表示で同一内容の重複行を除外し、チャンネル名を優先表示するよう改善
- URL単体投稿・極短文・区切り線投稿に品質減点を適用し、メモリ検索の文脈適合率を向上
- メモリ検索の最終採用で同一チャンネル・同一内容の重複候補を除外し、文脈ノイズを低減
- `MENTION_ASK_ENABLED` を追加し、Botメンション経由の質問応答ルートを実装（/askと同等の応答生成を利用）
- `/memory_status` コマンドを追加し、Discord上からギルドメモリの保存件数と主要コレクション件数を確認可能にした
- `/runcli` コマンドを追加し、Discordボタン（承認/拒否）経由で許可済みCLIを実行できるHitL運用を実装
- `/runcli` 承認処理で必要な `asyncio` import漏れを修正
- 参照メモリ出典の日時表示をJSTへ統一し、運用時の時刻解釈のズレを軽減
- `/runcli` 承認ボタンをephemeral表示から通常表示へ変更し、複数承認者でのHitL運用を成立させた
- `/runcli` の監査ログ出力を追加し、request/approve/reject/execute（exit code・結果プレビュー）をJSON Linesで永続化
- `/readurl` コマンドを追加し、Readerツールの単体検証をDiscord上で実行可能にした
- `/deepdive` コマンドを追加し、source指定（auto/github/reddit/youtube/x）で深掘りツールを直接検証可能にした
- `.env.example` に `RUNCLI_AUDIT_LOG_PATH`、検索キャッシュ制御（`SEARCH_CACHE_TTL_SEC` / `SEARCH_COOLDOWN_SEC`）を追記

### 運用基盤

- Dockerfile と docker-compose.yml を整備
- ルート基準のsrcレイアウトへ再編
- `.env.example` を追加し、`.env` はGit管理外へ変更
- `data/chromadb` 永続化前提の構成へ変更

### ドキュメント

- DESIGNにDocker必須運用を反映
- DESIGNのパス記述をsrcレイアウトに更新
- READMEにDiscord Botセットアップ手順を追加
- READMEのn8n章から本番導入ガイドへの参照導線を追加
- READMEへn8n workflowテンプレートのCUI import手順を追加
- change logの正式記録先を本ファイルに統一
