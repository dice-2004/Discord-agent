# Change Log

このファイルを本プロジェクトの正式な変更履歴として運用します。

## 2026-03-31

### 仕様策定・明確化

- Phase 1の未定義事項A〜Gを具体仕様として明文化
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

### 運用基盤

- Dockerfile と docker-compose.yml を整備
- ルート基準のsrcレイアウトへ再編
- `.env.example` を追加し、`.env` はGit管理外へ変更
- `data/chromadb` 永続化前提の構成へ変更

### ドキュメント

- DESIGNにDocker必須運用を反映
- DESIGNのパス記述をsrcレイアウトに更新
- READMEにDiscord Botセットアップ手順を追加
- change logの正式記録先を本ファイルに統一
