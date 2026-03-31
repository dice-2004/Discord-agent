
## N100最適化 拡張型Discord AIエージェント開発プロジェクト

あなたは優秀なシニアPython・AIアーキテクトです。本仕様書を熟読し、Discordをインターフェースとした「マルチエージェント・システム」の設計と実装を行ってください。
また、必ず守ることはこちらです。

①change logを付けること

②仕様が変わったらこの仕様書を第一に変更すること

③何を行ったか、どのようなコードか、初学者にわかるように解説すること

④ツールの追加は容易にできるようなコードにすること

---

## 0. 最重要実装方針（最初に必ず守ること）

以下は本仕様書全体に優先する**最上位ルール**です。

### 0.1. 今回の目的

本プロジェクトは、**実際に起動可能な形で継続的に安定実装すること**を目的とします。
将来構想を見越した疎結合設計は必要ですが、**安全性・運用性を損なう先回り実装は避けること**を強く求めます。

### 0.2. 実装の原則

* **「まず確実に動く最小構成」**を優先すること
* **低メモリ・低CPU・低常駐負荷**を最優先すること
* **同期ブロッキング処理を極力避ける**こと
* すべての外部依存は、**将来差し替えやすいモジュール構造**にすること
* **Botが1つの例外で落ちない設計**にすること
* **コードは実行可能な完全版**を出力し、省略・擬似コード・TODO残しをしないこと

### 0.3. 今回「実装しないもの」

以下は将来実装の候補として扱い、現時点で明示要請がある場合のみ実装対象とします。

* Research Agent の実装
* `gemini CLI` を使ったサブプロセス調査機構
* Reader / Markdown抽出ツールの実装
* Agent-Reach の実装
* ローカルCLI実行ツールの実装
* Discord Interactive Button によるHitL承認機構
* Eternal Explorer Agent の実装
* Web UI、管理画面、REST APIサーバー
* スケジューラーやバックグラウンドワーカーの常駐追加
* n8n Webhook 連携ツール、Discord過去ログ検索ツールの本実装
* カスタムドキュメント(PDF等)の読み込み処理、SQLiteの構築・運用

※ただし、**将来的に追加しやすい拡張ポイント（抽象化・関数分離・クラス分離）**は設計に含めてよい。

### 0.4. 実装優先モード（2026-03-31追記）

ユーザーの最新指示により、開発停滞を避けるために以下を許可する。

* フェーズ境界に縛られず、必要なツール実装を先行して進める
* ただし安全性・低負荷・疎結合設計の原則は維持する
* 仕様変更時は本書を先に更新し、次に `docs/change_log.md` を更新する

---

## 1. プロジェクトの全体ビジョンと制約事項

本システムは、ユーザーの「第二の頭脳（パーソナルアシスタント）」として機能し、日常のタスクから、最終的には**「ユーザーの代理として永遠に研究・検証を続ける自律システム」**までを担います。

* **稼働環境:** N100ミニPC (Proxmox上のLXCコンテナ: Debian)。Minecraftサーバーと同居するため、**「低メモリ消費」「不要な常駐プロセスの排除」「I/O待ちの非同期化」**がインフラ側の絶対条件です。
* **拡張性:** ルーター型エージェント（Orchestrator）を基盤とし、質問の種類に応じて適切なTool（道具）を使い分ける疎結合なアーキテクチャとします（Google公式SDK直呼び出しを許容）。

### 1.1. 非機能要件（追記）

本プロジェクトでは、機能実装と同等以上に以下を重視してください。

* **省メモリ性:** 不要なオブジェクト保持・大量ログ・巨大キャッシュを避ける
* **耐障害性:** API失敗・検索失敗・空レスポンスでもBot全体は継続動作する
* **保守性:** ファイル責務を明確化し、将来の差し替えを容易にする
* **可観測性:** 最低限のログ出力（起動、質問受付、検索実行、保存失敗、LLM失敗）を入れる
* **設定分離:** APIキーやトークンは必ず `.env` から読むこと

---

## 2. コア・アーキテクチャ（3層のエージェント分離）

ユーザー体験（UX）とAPI制限（RPD）の制約を両立し、将来の自律稼働を見据えて、タスクの重さに応じて3つのエージェント階層を定義します。

### 2.1. Main Agent (同期・即答・ルーティング担当)

* **インターフェース:** `discord.py` によるDiscord Bot（`/ask` コマンド、またはメンションに反応）。
* **LLMエンジン:** `Gemini 3.1 Flash Lite` API (軽量・高速・1日500リクエストの無料枠を使用)。
* **役割:** 数秒〜20秒で完結するタスク（簡単な検索、URL要約、ローカル状態確認）を実行し即答する。重いタスクを検知した場合は、後述のResearch Agent等に処理を丸投げする。

### 2.2. Research Agent (非同期・深掘り調査担当 / 将来実装)

* **インターフェース:** Main Agentからサブプロセス（バックグラウンドジョブ）として起動される。
* **LLMエンジン:** Google公式 `gemini CLI` ツール。
* **認証基盤:** "Sign in with Google" (OAuth) 済みのトークンを使用（巨大な無料枠で上位モデルを利用）。
* **役割:** 1分〜1時間程度かかるタスク（複数サイトの横断、Reddit/GitHubの深掘り）を実行。完了後、Discordに長文レポートを非同期で通知して終了する。

### 2.3. Eternal Explorer Agent (永久代理研究エージェント / 将来構想・現段階では実装しない)

* **概要:** 大学生であるユーザーの「代理研究者」として、特定のテーマについて終了指示が出るまで永遠に調査・検証・考察を続ける最上位エージェント。
* **アーキテクチャ設計思想:** LangGraph等のステートマシンを用い、「計画 → 探索 → 評価 → 軌道修正」の無限ループを構築する。API制限を回避するため、適宜スリープ（待機時間）を挟みながら数日〜数週間単位で稼働し、定期的にDiscordへ「進捗レポート」を投下する。
* **※注記:** 本エージェントは将来的な拡張目標であり、現時点の標準実装には組み込みませんが、コアシステムはこの独立した無限ループプロセスを将来的に呼び出せる疎結合な設計としてください。

### 2.4. 現時点での実装上の解釈（追記）

現時点では、この3層のうち**常時動作の中心は Main Agent**です。
ただし、`src/discord_ai_agent/core/orchestrator.py` の設計においては、将来以下を差し込めるようにしてください。

* 重い依頼を判定するルーティングフック
* 将来的な `dispatch_to_research_agent(...)` 相当の拡張ポイント
* 永続ジョブに渡すための入力ペイロード設計の余地

※ ただし **ダミー実装・未使用コードの大量追加は不要**です。
「あとから追加しやすい構造」に留めてください。

---

## 3. Tool（道具箱）の6本柱設計

「万能な1つの検索ツール」ではなく、役割を明確に分けた以下のツール群をMain Agent（Orchestrator）に持たせます。

1. **一般Web検索ツール (候補発見)**

   * **概要:** 「まず何を見るべきか探す」担当。DuckDuckGo Search API等を使用し、上位URLのリストを取得する。
2. **Reader / Markdown抽出ツール (本文読解)**

   * **概要:** 「見つけた先を綺麗に読む」担当。Jina Reader API (`https://jina.ai/reader/`) などを利用し、対象URLのHTMLノイズを除去してMarkdown化する。
3. **特殊ソース深掘りツール [Agent-Reach]**

   * **概要:** GitHub, Reddit, X, YouTubeなどをAIが読める形で取得する特化型ツール。
   * **リファレンス:** オープンソースの `Agent Reach` (`https://github.com/phodal/agent-reach` 等) の思想を組み込み、**必要な時（SNSの反応やOSSのIssueを調べたい時）だけ発火**させる。
4. **ローカルCLIツール (自宅サーバー運用)**

   * **概要:** `docker ps` などのホストシステム情報取得。
   * **【絶対遵守のセキュリティ（HitL）】:** コマンドの「実行」を伴う操作は、必ずDiscordのInteractive Button（承認/拒否）を生成し、管理者ユーザーが承認した場合のみ実行される設計（Human-in-the-Loop）とすること。
5. **Discord過去ログ検索ツール [将来実装]**

   * **概要:** RAGによる受動的な記憶引き出しとは別に、AIが自発的に特定のキーワードやユーザー名で過去のDiscordログを検索しに行くツール。
6. **外部自動化・アクションツール [n8n Webhook]**

   * **概要:** 「外部サービスへ行動を起こす」担当。ローカルで稼働するn8nコンテナのWebhookエンドポイントを叩き、Googleカレンダーへの予定追加やメール送信などの複雑なAPI操作を委譲する。
   * **※注記:** `action + parameters` の単一Webhook契約を維持し、アクションを段階的に本実装する。

### 3.1. 現時点で実装するツール範囲（追記）

実装優先モードでは、上記6本柱を段階的に実装してよい。

#### 現時点で必須のツール要件

* `src/discord_ai_agent/tools/search_tools.py` に分離すること
* Orchestrator から呼び出せる独立関数として実装すること（将来のFramework差し替えを容易にする）
* 検索結果は**上位数件のタイトル / URL / 概要**をテキストとして返すこと
* 検索失敗時は例外を握りつぶさず、**LLMが扱える安全な失敗メッセージ**を返すこと
* レスポンスは**長すぎない**ように整形すること（Discord向け）

#### 現時点でまだ実装しないもの

* Reader API 呼び出し
* Agent-Reach 連携
* YouTube / Reddit / GitHub の専用取得器
* CLIコマンド実行系
* Discord過去ログ検索の専用ツール化
* n8n Webhookの未実装アクション（`add_notion_memo` / `append_sheet_row` / `send_email` / `backup_server_data`）

※実装優先モードでは、HitL安全策（承認トークン・許可コマンド制限など）を伴う範囲でCLI系ツールを先行実装してよい。

### 3.2. n8n連携ツールの標準設計（追記）

n8n連携は、将来の拡張性と安全性を両立するため、以下の共通インターフェースを標準とする。

* **ツール名:** `execute_n8n_workflow`
* **責務:** 外部サービス（Googleカレンダー、メール等）への副作用を伴う操作を委譲する。
* **AIが生成する引数構造:**

```json
{
   "action": "add_calendar_event",
   "parameters": {
      "title": "ゼミのミーティング",
      "start_time": "2026-04-10T15:00:00+09:00",
      "end_time": "2026-04-10T16:00:00+09:00",
      "description": "任意"
   }
}
```

* **必須トップレベルキー:**
   * `action`: 実行アクション名
   * `parameters`: アクションごとのパラメータオブジェクト

* **初期アクション定義:**
   1) `add_calendar_event`
       * 必須: `title`, `start_time`, `end_time`
       * 任意: `description`
   2) `get_calendar_events`
       * 必須: `time_min`, `time_max`
   3) `add_notion_memo`
      * 必須: `title`, `content`, `category`
   4) `append_sheet_row`
      * 必須: `sheet_name`, `column_data`
   5) `create_github_issue`
      * 必須: `repository`, `title`, `body`
      * 実行条件: n8n環境に `GITHUB_TOKEN`（`repo` 権限）を設定していること
   6) `backup_server_data`
      * 必須: `target`
   7) `send_email` (任意・将来用)
      * 必須: `to_address`, `subject`, `body`

* **検証ルール:**
   * actionは許可リスト (`N8N_ALLOWED_ACTIONS`) に含まれるもののみ実行。
   * 必須キーは action別スキーマ (`N8N_ACTION_REQUIRED_FIELDS`) で検証。
   * 日時はISO8601形式を要求し、パース不能時は実行しない。
   * 検証エラー時は副作用を発生させず、ユーザーに不足項目を返す。

---

## 4. 記憶（Context）とデータベース設計

インメモリの圧迫を防ぐため、以下の2段構えで記憶を管理します。

1. **初期プロファイル・ルールの注入 (Gemini Skills)**

   * **概要:** ユーザーの性格や前提知識はVector DBに入れず、`gemini-skills` (`https://github.com/google-gemini/gemini-skills`) の思想に則り、Markdownファイル (`data/profiles/initial_profile.md`) からプロンプトとして直接静的に注入します。
2. **会話履歴の蓄積 (ChromaDB)**

   * **概要:** 日々のDiscord会話履歴のみをChromaDBにベクトル保存し、必要に応じてRAGで引き出します。
   * **【権限分離】:** Discordの「チャンネルID」または「サーバーID」をコレクション名のキーとし、身内用サーバーから自分専用の記憶に絶対にアクセスできないよう厳格に分離してください。
   * **【方向付き境界（任意）】:** `DIRECTIONAL_MEMORY_ENABLED=true` 時は `PERSONAL_GUILD_ID` と `FAMILY_GUILD_IDS` を参照し、「個人サーバー -> 身内サーバー参照のみ許可、逆方向と身内間参照は禁止」を適用する。
3. **カスタムドキュメントのRAG (大学資料など / 将来拡張)**

   * **概要:** Discordの会話だけでなく、特定のディレクトリに配置されたPDFやテキストファイル（大学のシラバス、研究室のマニュアル等）を読み込み、専用コレクションとしてChromaDBにベクトル保存する拡張枠を想定する。
4. **システム・権限管理DB (SQLite / 将来実装)**

   * **概要:** 現時点では必須ではないが、将来的な「チャンネルごとのアクセス権限マップ」や「HitLの承認待ちステータス」を永続化するためのRelational DBとしてSQLiteを採用可能にする。設計上、組み込める余白を残すこと。
5. **ユーザー・ペルソナ長期記憶 (第二の自分化 / 継続拡張)**

   * **概要:** ユーザーの性格・価値観・好み・長期目標・運用ルールを、会話RAGとは別に長期記憶として保持し、回答の一貫性を高める。
   * **保存対象:** 好み（文体/優先順位/NG表現）、固定情報（所属・学習分野）、運用方針（まず結論/簡潔回答など）、長期目標（学習計画/研究テーマ）、定常タスク（定例作業）。
   * **保存対象外:** APIキー、パスワード、秘密鍵、クレジットカード情報、本人が保存拒否した情報。

### 4.1. 現時点でのメモリ設計の具体条件（追記）

`src/discord_ai_agent/core/memory.py` では以下を満たしてください。

* **保存単位:** Discordメッセージ単位
* **分離キー:** 最低でも `channel_id` ベースで分離
* **推奨:** 可能であれば `guild_id + channel_id` を組み合わせた論理キー
* **保存内容:** ユーザー発話 / AI応答 / タイムスタンプ / メタデータ
* **検索用途:** 現在の質問に関連する過去会話の数件を取得する
* **履歴取り込み:** 起動時にチャンネル履歴をバックフィルし、以後は`on_message`で全会話を継続保存する
* **取り込み対象拡張:** 起動時バックフィルではテキストチャンネルに加えてアーカイブ済みスレッドも取得対象に含める
* **時刻整合:** 保存時刻は取り込み時刻ではなくDiscordメッセージの作成時刻を優先して保持する
* **遅延対策:** `/ask` 処理とは分離し、履歴取り込みで応答遅延を増やさない
* **前提条件:** 全会話収集にはDiscord Developer PortalでMessage Content Intentを有効化し、`DISCORD_ENABLE_MESSAGE_CONTENT_INTENT=true` を設定する
* **参照範囲:** 回答時の既定参照はチャンネル限定ではなく、同一ギルド全体（`MEMORY_RETRIEVAL_SCOPE=guild`）とする
* **検証モード:** 回答末尾に参照メモリを表示する `MEMORY_RESPONSE_INCLUDE_EVIDENCE` をサポートする
* **可読性:** 参照メモリ表示ではチャンネルIDではなくチャンネル名を優先表示し、同一内容の重複行を抑制する
* **日時表示:** 参照メモリ表示の時刻はJST表記へ統一し、Discord上での確認を容易にする
* **品質補正:** URL単体投稿や極端に短い定型投稿を再ランク時に減点し、文脈性の高い記録を優先する
* **重複抑制:** 同一チャンネル・同一内容の重複候補は最終採用時に除外し、履歴文脈の多様性を保つ
* **再同期:** 既存データをギルド全体インデックスへ再構築する場合は `MEMORY_BOOTSTRAP_FORCE_REINDEX=true` を一時利用する
* **保存失敗時:** Bot全体を落とさず、ログに残して継続
* **空データ時:** 正常に無視できること

### 4.2. メモリに保存しないもの（追記）

以下は現時点では保存対象外としてよいです。

* 添付ファイルの中身
* 画像埋め込み
* 音声
* URL本文のフルテキスト
* Discordのリアクション履歴

### 4.3. 第二の自分化メモリ要件（追記）

`第二の自分（専用秘書）` を目指すため、以下の要件を追加する。

* **メモリ階層:**
   1) 短期文脈（現在スレッド）
   2) 会話RAG（Discordログ）
   3) 長期ペルソナ記憶（ユーザー固有プロファイル）
* **参照優先順位:** 回答時は「長期ペルソナ記憶 → 直近会話 → 検索結果」の順で矛盾チェックし、競合時は最新のユーザー明示指示を優先する。
* **更新方針:** 会話から自動抽出した候補は即確定せず、重要情報（価値観/長期目標/生活情報）は確認フラグを立てる。
* **同意ポリシー:** 個人プロファイル保存は opt-in を基本とし、ユーザーが明示的に無効化できること。
* **削除権:** ユーザー要求で、単一項目削除・カテゴリ削除・全消去を実行できること。
* **説明可能性:** 可能な限り「この回答で参照したユーザー情報」を要約表示できること。
* **分離要件:** ペルソナ記憶は `user_id` を主キーにし、他ユーザーと混在させない。
* **耐障害性:** プロファイル記憶の読み書き失敗時もBotは継続動作し、通常回答へフォールバックする。

### 4.4. Gemini Web会話エクスポート取り込み方針（追記）

* **目的:** Web版Geminiでの過去会話を取り込み、第二の自分化に必要な性格・嗜好・長期目標の初期値を補完する。
* **対応形式（初期）:** `json`, `csv`
* **補足:** `mpeg` は通常テキスト会話エクスポート形式ではないため、会話本文としては直接取り込まず、必要なら別途文字起こし後に取り込む。
* **取り込み方針:**
   * raw全文をそのまま長期記憶へ投入せず、候補factを抽出して保存する。
   * 候補factは `source=gemini_export` として記録する。
   * 高重要項目（価値観・長期目標）は確認フラグ付きで保存する。
* **安全方針:**
   * APIキーや認証情報らしき値は取り込み時に除外する。
   * 取り込みジョブ失敗時もBot本体は継続動作する。

---

## 5. 開発ロードマップ（継続実装）

* **現在の中核:** ベースBotとOrchestrator基盤を維持しつつ、運用で必要なツールを段階的に追加する。

   * `discord.py` のセットアップ。
   * Gemini 3.1 Flash Lite APIをGoogle公式SDKで利用し、Orchestrator主導でツール連携するエージェントを構築。
   * Gemini Skills（静的Markdown読み込み）とChromaDB（会話履歴保存）の統合。
   * 一般Web検索ツールのモジュール化実装。
* **次段の拡張候補:** Research Agent (`gemini CLI` サブプロセス)、Reader / Agent-Reach、Discord過去ログ検索ツール。
* **運用拡張候補:** ローカルCLIツールのHitL強化、SQLiteによる承認待ち・権限情報管理。
* **秘書化拡張候補:** ペルソナ抽出器、プロファイル確認フロー、タスク管理（締切/優先度）、行動提案の定期サマリ。
* **将来構想:** n8n連携、Agent P2Pプロトコル連携、および **Eternal Explorer Agent（無限ループ自律研究）** の構築。

### 5.1. 完成定義（追記）

今回の成果物は、**「ローカル起動でき、Discord上で質問に応答できる安定実装」**であること。

単なるサンプルや概念実装ではなく、**`.env` を設定すれば起動可能なコード**を出力すること。

---

## 6. 具体的な実装指示（あなたへのタスク）

上記のアーキテクチャを完全に理解した上で、現時点で機能する以下のディレクトリ構成とコアコードを生成してください。

**【ディレクトリ構造指定】**

```text
AI-agent-bot/
│
├── .gitignore
├── .dockerignore
├── README.md
├── requirements.txt
├── .env.example                 # 実運用では .env を作成（Git管理外）
├── Dockerfile
├── docker-compose.yml
│
├── data/
│   ├── profiles/
│   │   └── initial_profile.md  # システムプロンプトとして読み込ませる性格・ルール設定
│   └── chromadb/               # ChromaDBの永続化ストレージ (自動生成・Git管理外)
│
├── src/
│   └── discord_ai_agent/
│       ├── __init__.py
│       ├── main.py             # Botの起動、イベントリスナー
│       ├── core/
│       │   ├── __init__.py
│       │   ├── orchestrator.py
│       │   └── memory.py
│       └── tools/
│           ├── __init__.py
│           └── search_tools.py
│
└── docs/
   └── DESIGN.md
```

---

## 7. 実装上の厳密ルール（追記・重要）

コーディング時は以下を厳守してください。

### 7.1. Python / 設計ルール

* Python 3.11 以上を前提とすること
* 型ヒントを可能な範囲で付与すること
* ファイルごとの責務を明確にすること
* グローバル状態を最小限にすること
* クラスが不要なら無理にクラス化しないこと
* ただし、将来差し替えるもの（LLM・Memory・Tool群）は整理して実装すること

### 7.2. 非同期ルール

* Discordイベント処理は `async/await` を正しく用いること
* 外部I/Oを伴う処理は可能な限り非同期にすること
* 重い同期処理でイベントループを塞がないこと

### 7.3. エラーハンドリング

* 以下でBotが落ちないこと

  * LLM APIエラー
  * 検索失敗
  * ChromaDB保存失敗
  * `.env` 設定漏れ
  * Discord送信失敗
* ユーザー向けには簡潔な失敗メッセージを返し、詳細はログに出すこと

### 7.4. Discord運用ルール

* `/ask` コマンドを実装すること
* 可能ならメンション応答も含めてよいが、**最低限 `/ask` が確実に動くことを優先**
* 実装優先モードでは `MENTION_ASK_ENABLED=true` 時にBotメンションでも質問応答できるようにする
* 実装優先モードでは `/runcli` を追加し、Discordボタン（承認/拒否）を経由したCLI実行を可能にする
* `/runcli` の承認UIは複数承認者が押せるよう、ephemeralではなく通常メッセージで提示する
* 実装優先モードでは `/readurl` と `/deepdive` を運用検証コマンドとして追加してよい
* 実装優先モードでは `/logsearch` を追加し、キーワードによるDiscord過去ログ検索を明示実行できるようにしてよい
* `/logsearch` は結果ごとに簡易スコア（一致度/新しさ）を表示できるようにする
* `/runcli` は申請・承認/拒否・実行結果（exit code含む）を監査ログへ記録する
* 実装優先モードでは `/runcli_audit` を追加し、直近監査イベントをephemeralで確認できるようにしてよい
* `/runcli_audit` は `requested/approved/rejected/executed` などのevent種別フィルタに対応できるようにする
* 実装優先モードでは `/n8n_action` を任意追加し、許可済みactionのみWebhookへ委譲できるようにしてよい
* `/n8n_action` は action別の必須キー検証（payloadスキーマの最小検証）に対応できるようにする
* 実装優先モードでは `/profile_show` `/profile_set` `/profile_forget` を任意追加し、長期ペルソナ記憶を安全に更新・削除できるようにしてよい
* Discordの**1メッセージ文字数制限**を意識し、必要なら適切に切り詰めること
* 長文レスポンスで落ちないようにすること

### 7.5. LLMプロンプト設計ルール

* `initial_profile.md` の内容を毎回のシステム文脈として読み込むこと
* 検索ツールは、必要時のみ使う前提で組み込むこと
* 「わからないときは無理に断定しない」ような安全性を持たせること
* 現時点では、**過剰に複雑なマルチステップ自律ループを常時動作させないこと**

---

## 8. 受け入れ条件（Acceptance Criteria）【追記・非常に重要】

以下を満たした場合のみ、完成とみなします。

### 8.1. 起動条件

* `.env` に `DISCORD_TOKEN` と `GEMINI_API_KEY` を設定すれば起動できる
* 起動時に必要ディレクトリが不足していても、必要に応じて作成または安全にエラー化できる

### 8.2. Discord動作条件

* `/ask` コマンドで質問を送れる
* BotがDiscord上で回答を返す
* 例外発生時もプロセスが落ちない

### 8.3. Orchestrator条件

* `src/discord_ai_agent/core/orchestrator.py` がMain Agentとして機能する
* `initial_profile.md` を読み込んで応答に反映する
* 検索ツールを組み込める

### 8.4. メモリ条件

* 会話がChromaDBに保存される
* 検索スコープ（チャンネル限定 / 同一ギルド）を設定で制御できる
* スコープ外の記憶を混ぜない
* ユーザー固有の長期ペルソナ記憶を `user_id` 単位で分離できる
* ユーザーが記憶の表示・更新・削除を実行できる設計余地を持つ

### 8.5. コード品質条件

* すべての指定ファイルが**完全コード**として出力される
* 擬似コードや「ここに処理を書く」は禁止
* import不足・未定義変数・未使用の中核処理がないこと

---

## 9. 出力形式の厳密指定（追記・最重要）

以下の形式でのみ出力してください。

### 9.1. 最初に出すもの

まず最初に、以下を**簡潔に**出力すること。

1. **理解の宣言**

   * 「同期・非同期・永久探索の3層エージェント設計」
   * 「検索と読解ツールの分離」
   * 「Gemini Skillsによる静的プロファイル注入」
     の意図を理解したことを簡潔に述べる

2. **実装方針の要約**

* 今回の範囲を明示し、過剰な先回り実装を避けること
   * Main Agent 中心の最小構成であること
   * 将来拡張可能な疎結合設計にすること

### 9.2. 次に出すもの

以下をこの順で出力すること。

1. `requirements.txt`
2. `src/discord_ai_agent/main.py`
3. `src/discord_ai_agent/core/orchestrator.py`
4. `src/discord_ai_agent/core/memory.py`
5. `src/discord_ai_agent/tools/search_tools.py`

### 9.3. 出力ルール

* 各ファイルは**見出し + 完全コードブロック**で出力すること
* **コードは省略禁止**
* **コメントは必要最小限でよいが、要所には入れてよい**
* **ファイルの中身がそのまま保存できる完全形**にすること
* 余計な設計論・長い前置き・未実装説明を増やしすぎないこと

---

## 10. 明示的な禁止事項（追記）

以下は禁止です。

* ユーザー明示要求なしにDocker構成やcomposeを追加すること
* Flask/FastAPI等の別サーバーを勝手に立てること
* 同期HTTP中心の重い実装にすること
* 必要以上に大規模なフレームワーク化をすること
* 「とりあえず動くが壊れやすい」モノリシックコードにすること
* 要件にないデータ収集・永続化を勝手に増やすこと

---

## 11. 最終タスク

以上をすべて踏まえ、**N100上で現実的に動作し、将来拡張に素直に伸ばせる完成コード**を生成してください。

**【出力の要件】**

1. **理解の宣言:** 「同期・非同期・永久探索の3層エージェント設計」「検索と読解ツールの分離」「Gemini Skillsによる静的プロファイル注入」の意図を理解したことを簡潔に宣言してください。
2. **依存関係:** `requirements.txt` の内容を出力してください。
3. **コード生成:**

   * `src/discord_ai_agent/main.py`
   * `src/discord_ai_agent/core/orchestrator.py`
   * `src/discord_ai_agent/core/memory.py`
   * `src/discord_ai_agent/tools/search_tools.py`
     の4つのファイルの完全なPythonコードを出力してください。非同期処理（`async/await`）を適切に使用し、チャンネルIDごとの記憶分離ロジックを必ず含めてください。

> **もしライブラリの最新バージョン差異により LangChain / Gemini 連携実装が不安定な場合は、過剰に抽象化せず、「安定して動作する最小の呼び出し実装」を優先してください。**

---

## 12. 現行仕様確定（未定義項目の補完）

本章は、A〜Gの確認結果を反映した**実装時の確定仕様**です。

### 12.1. initial_profile.md の扱い（Aへの回答）

#### 12.1.1. ファイル有無の挙動

* `data/profiles/initial_profile.md` が存在しない場合、**起動は継続**する
* その際は `WARNING` ログを1回出力する（例: `initial_profile.md not found; continuing without static profile`）

#### 12.1.2. 最大サイズ

* 推奨上限を **12,000文字（約6k〜8k tokens未満を想定）** とする
* 上限超過時は先頭から利用し、末尾を切り詰める
* 切り詰め時は `WARNING` ログを出す

#### 12.1.3. 推奨テンプレート（改善提案1）

```markdown
# Assistant Profile

## Role
- あなたはユーザーの第二の頭脳として、正確で簡潔に支援する。

## Communication Style
- 日本語で回答する。
- 不明点は断定せず、必要なら確認質問を1〜3個に絞る。

## Priorities
1. 正確性
2. 実行可能性
3. 省リソース

## Tool Usage Policy
- まず内部知識で回答可能か判定する。
- 最新情報・出典が必要な場合のみWeb検索ツールを使う。

## Safety
- APIキー、トークン、個人情報らしき文字列を出力しない。

## Output Rules
- Discordの可読性を優先し、冗長な前置きを避ける。
```

### 12.2. 一般Web検索ツール仕様（Bへの回答）

現行の固定値は以下とする。

* 取得件数: **最大5件**
* 応答整形: 1件あたり「タイトル / URL / 概要（最大180文字）」
* ツール全体の文字数上限: **2,500文字**（超過分は切り詰め）
* タイムアウト: **10秒**
* リトライ: **最大2回（指数バックオフ 1秒, 2秒）**
* 失敗時の返却: LLMが扱える平文メッセージを返す（例: `検索に失敗しました。時間をおいて再試行してください。`）

#### 12.2.1. 検索結果フォーマット例（改善提案3）

入力例:

* `Python async/await 最新情報`

出力例:

```text
【Web検索結果】
1. Python 3.x Documentation - asyncio
URL: https://docs.python.org/...
概要: 非同期I/Oの公式ドキュメント。イベントループ、Task、awaitの基本と実践例を解説。

2. ...
```

### 12.3. ChromaDBメモリ設計（Cへの回答）

#### 12.3.1. 分離キーとコレクション命名

* 論理分離キーは **`guild_id + channel_id`** を採用
* コレクション名: `mem_g{guild_id}_c{channel_id}`
* DM等で `guild_id` が無い場合: `mem_gdm_c{channel_id}`

#### 12.3.2. 保存スキーマ（改善提案2）

| フィールド | 型 | 必須 | 用途 | 例 |
| --- | --- | --- | --- | --- |
| id | str | 必須 | レコードID | `1735600000-user-123456` |
| role | str | 必須 | 発話者種別 | `user` / `assistant` |
| content | str | 必須 | 発話本文 | `非同期処理を教えて` |
| timestamp | str (ISO8601) | 必須 | 記録時刻 | `2026-03-31T10:20:30+09:00` |
| user_id | str | 必須 | DiscordユーザーID | `123456789012345678` |
| message_id | str | 任意 | DiscordメッセージID | `234567890123456789` |
| metadata | dict | 任意 | 拡張情報 | `{\"source\":\"discord\"}` |

#### 12.3.3. 検索ルール

* 取得件数: **上位4件**
* 対象: 同一コレクション（同一 `guild_id + channel_id`）のみ
* 空データ時: 空配列を返して通常継続

### 12.4. Discordコマンド仕様（Dへの回答）

* 必須コマンドは `/ask` のみ
* 形式: `/ask question:<string>`
* メンション応答は**任意**（実装しても良いが必須ではない）
* 運用デバッグ用に `/memory_status`（ephemeral）を実装し、保存件数と主要コレクション件数を確認可能にする
* 実装優先モードでは `/readurl` と `/deepdive` を任意追加し、Reader/DeepDiveツールの単体検証をDiscord上で行えるようにしてよい
* 実装優先モードでは `/logsearch` を任意追加し、`scope=channel/guild` を指定して過去ログ候補を表示できるようにしてよい
* 実装優先モードでは `/runcli` の監査ログ（request/approve/reject/execute）をJSON Linesで永続化してよい
* 実装優先モードでは `/runcli_audit` を任意追加し、監査ログの末尾N件を安全に参照できるようにしてよい
* 実装優先モードでは `/n8n_action` を任意追加し、`N8N_ALLOWED_ACTIONS` に含まれる action のみを実行対象とする
* `/runcli_audit` は eventフィルタ引数を受け取り、絞り込み後の末尾N件を返せるようにしてよい
* `/n8n_action` は `N8N_ACTION_REQUIRED_FIELDS` に基づき action別必須キーを検証してから実行する
* `BOT_GUILD_ID` は必須設定
* 必要に応じて `ALLOWED_GUILD_IDS` で許可ギルドを列挙し、該当ギルド以外では `/ask` を拒否する

### 12.5. 失敗時挙動・再試行・分割送信（Eへの回答）

#### 12.5.1. LLM/検索の再試行戦略（改善提案4）

* Gemini API 呼び出し:
   * タイムアウト: **30秒**
   * リトライ: **最大2回（1秒, 2秒バックオフ）**
* Web検索呼び出し:
   * タイムアウト: **10秒**
   * リトライ: **最大2回（1秒, 2秒バックオフ）**

#### 12.5.2. Discord送信制御

* 1メッセージ上限を **1900文字** 目安で分割送信（安全マージン確保）
* 分割は段落優先、難しい場合は固定長分割
* 総量が **15,000文字超** の場合は、要約を本文送信し、全文はテキストファイル添付へフォールバック

### 12.6. ログ設定（Fへの回答）

現行のデフォルトを以下とする。

* 既定ログレベル: **INFO**
* `.env` の `LOG_LEVEL` で上書き可能
* 最低限記録するイベント:
   * 起動成功/失敗
   * `/ask` 受信
   * 検索ツール実行開始/失敗
   * LLM呼び出し失敗
   * メモリ保存失敗
   * Discord送信失敗

### 12.7. .env 仕様（改善提案5）

推奨 `.env` は以下。

```env
DISCORD_TOKEN=your_discord_token
GEMINI_API_KEY=your_gemini_api_key
LOG_LEVEL=INFO
BOT_GUILD_ID=123456789012345678
ALLOWED_GUILD_IDS=123456789012345678,223456789012345678,323456789012345678,423456789012345678
DISCORD_ENABLE_MESSAGE_CONTENT_INTENT=false
CHROMADB_PATH=./data/chromadb
MEMORY_BOOTSTRAP_ON_READY=true
MEMORY_BOOTSTRAP_MAX_PER_CHANNEL=0
MEMORY_BOOTSTRAP_BATCH_SIZE=200
MEMORY_BOOTSTRAP_FORCE_REINDEX=false
MEMORY_BOOTSTRAP_INCLUDE_ARCHIVED_THREADS=true
MEMORY_BOOTSTRAP_ARCHIVED_LIMIT_PER_PARENT=0
MEMORY_RETRIEVAL_SCOPE=guild
MEMORY_TOP_K=8
MEMORY_RESPONSE_INCLUDE_EVIDENCE=false
MEMORY_RESPONSE_EVIDENCE_ITEMS=3
DIRECTIONAL_MEMORY_ENABLED=false
PERSONAL_GUILD_ID=
FAMILY_GUILD_IDS=
INITIAL_PROFILE_PATH=./data/profiles/initial_profile.md
SEARCH_MAX_RESULTS=5
SEARCH_TIMEOUT_SEC=10
SEARCH_CACHE_TTL_SEC=180
SEARCH_COOLDOWN_SEC=45
READER_TIMEOUT_SEC=12
READER_MAX_CHARS=5000
DEEP_DIVE_MAX_QUERIES=3
GEMINI_TIMEOUT_SEC=30
MAX_DISCORD_MESSAGE_LEN=1900
MENTION_ASK_ENABLED=true
MAX_TOOL_TURNS=3
MAX_REVIEW_TURNS=1
CLI_ALLOWED_COMMANDS=docker ps,docker compose ps,uptime,df -h,free -m
CLI_APPROVAL_TOKEN=change_me
CLI_APPROVER_USER_IDS=123456789012345678
RUNCLI_AUDIT_LOG_PATH=./data/audit/runcli_audit.jsonl
LOGSEARCH_DEFAULT_SCOPE=guild
RUNCLI_AUDIT_TAIL_DEFAULT=20
RUNCLI_AUDIT_EVENT_FILTER_DEFAULT=all
LOGSEARCH_INCLUDE_SCORE=true
LOGSEARCH_SCORE_OVERLAP_WEIGHT=0.7
LOGSEARCH_SCORE_RECENCY_WEIGHT=0.3
N8N_WEBHOOK_BASE_URL=http://n8n:5678/webhook/execute_n8n_workflow
N8N_ALLOWED_ACTIONS=add_calendar_event,get_calendar_events,add_notion_memo,append_sheet_row,create_github_issue,send_email,backup_server_data
N8N_ACTION_REQUIRED_FIELDS=add_calendar_event:title,start_time,end_time;get_calendar_events:time_min,time_max;add_notion_memo:title,content,category;append_sheet_row:sheet_name,column_data;create_github_issue:repository,title,body;send_email:to_address,subject,body;backup_server_data:target
N8N_WEBHOOK_TOKEN=
N8N_TIMEOUT_SEC=12
N8N_RETRY_COUNT=1
N8N_RETRY_BACKOFF_SEC=1
GITHUB_TOKEN=
PERSONA_MEMORY_ENABLED=true
PERSONA_MEMORY_INCLUDE_IN_PROMPT=true
PERSONA_MEMORY_REQUIRE_CONFIRMATION=true
PERSONA_MEMORY_MAX_FACTS=200
PERSONA_MEMORY_COLLECTION=persona_profiles
PERSONA_MEMORY_EVIDENCE_ITEMS=3
```

### 12.8. Google AI StudioのGrounding利用方針（提案への回答）

Google AI Studio/Geminiの「検索・地図グラウンディング」は、将来的に有効な選択肢です。

ただし現時点では、以下の理由で**既定採用しない**。

* 仕様上、検索ツールは `src/discord_ai_agent/tools/search_tools.py` で独立実装する方針
* 依存を増やしすぎると、N100運用時の障害切り分けが難しくなる
* 将来比較検証（DuckDuckGo vs Gemini Grounding）を行う余地を残したい

したがって現行はDuckDuckGoベースで固定し、将来オプションツールとして追加可能な拡張ポイントを残す。

### 12.9. 追加確定事項（2026-03-31反映）

* 大容量応答の添付ファイル名は **`ask_response.txt` 固定** とする
* `BOT_GUILD_ID` は **必須設定** とする
* 運用ギルドは「個人用 / 身内用(1) / 身内用(2) / 身内用(3)」など、複数IDを扱う想定とし、
   現行では **`ALLOWED_GUILD_IDS`（カンマ区切り）** で許可ギルドを定義する
* Web検索結果は同一ドメインが重複しないよう、上位から**ドメイン重複除外**して採用する

### 12.10. Docker実行前提（2026-03-31反映）

* 実装・検証・実運用は **Dockerコンテナ内で実行**する
* 現行では、以下を最小要件とする
   * `Dockerfile` を用意し、`python:3.11-slim` 系を利用
   * ルートディレクトリの `.env` を渡して起動可能であること
   * ルートの `data/chromadb` をボリューム永続化できる構成であること
* 追加の管理基盤（Kubernetes等）は現時点の対象外

### 12.11. 自律ツール判断方針（2026-03-31反映）

* Main Agentは、ユーザーの追加指示待ちではなく**自律的に検索要否を判断**する
* 最新性が必要な質問（例: 天気、ニュース、価格、障害情報）は検索を優先する
* 1回の質問に対し、必要に応じて**最大3クエリ**まで段階的に検索する
* 回答は「結論先出し」を原則とし、不要な確認質問は避ける
* 情報不足時は、何が不足しているかを短く明示し、取得できた根拠を優先して提示する
* 検索クエリは固定テンプレートだけに依存せず、**LLMが質問文から汎用的に計画**できる構造とする
* 検索計画に失敗した場合は、軽量なヒューリスティックにフォールバックして可用性を優先する

---

## 13. Change Log運用

変更履歴は [docs/change_log.md](docs/change_log.md) に追記して管理すること。
この仕様書に変更を加えた場合も、同時に [docs/change_log.md](docs/change_log.md) を更新する。
