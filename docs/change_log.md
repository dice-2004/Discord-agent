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

### 実装

- Discord BotのPhase 1実装を追加
- Main Agent、Orchestrator、Memory、Search Toolを実装
- `ask_response.txt` 添付方針を実装
- 同一ドメイン重複除外の検索整形を実装
- Gemini連携をGoogle公式SDK直呼び出しへ移行（LangChain依存を削減）
- Gemini tool callingの`thought_signature`問題を回避するため、Orchestrator側のツール判定・実行方式へ変更
- `search_tools.py` からLangChain `@tool` デコレータ依存を除去し、独立関数として再実装

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
