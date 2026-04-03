# 手動実施が必要な残テスト一覧

## 目的

この文書は、未実施テストのうち「GitHub Copilot 側の自動実行だけでは信頼性ある判定がしづらい項目」を、理由と概要つきで整理したものです。

## 件数サマリ

- 手動必須: 21件
- 自動継続可能: 0件
- この文書で整理している未実施残件の合計: 21件

補足:
- [docs/TEST_RESULTS.md](TEST_RESULTS.md) の未実施 21 件は、現時点では記入漏れではなく実未実施です。
- 現時点の未実施 21 件はすべて手動必須項目です。

## 手動必須項目（理由つき）

| ID | 理由 | 概要 |
|---|---|---|
| UT-030 | `/runcli` の承認UIはDiscord上のボタン操作が前提 | `/runcli` 実行時に承認ボタン付き投稿が出るか確認 |
| UT-031 | 承認ボタン押下イベントは実ユーザー操作が必要 | 承認者が approve を押し、実行・監査記録を確認 |
| UT-032 | reject ボタン押下イベントは実ユーザー操作が必要 | 承認者が reject を押し、拒否記録を確認 |
| UT-033 | 権限差分（承認者/非承認者）は複数ユーザー検証が必要 | 非承認者操作時に拒否されることを確認 |
| UT-034 | 監査表示コマンドは運用中の監査データとUI確認が必要 | `/runcli_audit` 既定表示の内容確認 |
| UT-035 | 監査のイベントフィルタは実監査データでの表示差分確認が必要 | `/runcli_audit event:approved` の絞り込み確認 |
| UT-036 | デバッグ権限差分は実ユーザー権限での確認が必要 | 非デバッグ担当者の `/debug_action` 拒否を確認 |
| UT-038 | `/debug_mention_probe` はDiscord実チャネルへの投稿結果確認が必要 | probe投稿と応答がチャンネルに残ることを確認 |
| UT-039 | `/debug_probe_tail` は実運用監査ログの表示確認が必要 | 直近probe監査が表示されることを確認 |
| CT-007 | `/runcli` 承認フロー全体はUIと操作の連鎖確認が必要 | request/approved/executed が追跡可能か確認 |
| CT-008 | 自律メンション + 監査は実メッセージ経路確認が必要 | 投稿・応答・監査が揃うことを確認 |
| MT-022 | `MENTION_REQUIRE_PREFIX` の判定は実 `on_message` 経路が必要 | 文中メンション時の非応答を確認 |
| MT-023 | 空メンション時の入力検証は実 `on_message` 経路が必要 | 「質問内容を書いてください」返却を確認 |
| MT-024 | 監査イベント（mention_answer_sent）は実メンション生成が必要 | メンション後に `/debug_probe_tail` で監査確認 |
| QLP-022 | `mention_quick` 高速経路は self_probe では通らない | `@bot 明日14時から15時に会議` で quick経路を確認 |
| QLP-023 | `mention_quick` タスク経路は self_probe では通らない | `@bot 明後日までにレポート提出タスク` を確認 |
| SEC-001 | runcli 承認トークン検証はボタン操作と認証状態が必要 | 不正トークンで承認時に拒否されるか確認 |
| SEC-004 | user_id 分離はユーザーA/Bの2アカウント検証が必要 | Aで保存したprofileがBに見えないことを確認 |
| SEC-006 | runcliタイムアウトは時間経過とUI無効化確認が必要 | 90秒放置後ボタン無効化を確認 |
| CFG-005 | profile系のephemeral応答はslash実行でのUI確認が必要 | `PERSONA_MEMORY_ENABLED=false` で `/profile_show` 文言確認 |
| CFG-006 | mention quick無効化確認は実メンション経路で検証が必要 | `MENTION_QUICK_CALENDAR_ENABLED=false` 時のLLM経由確認 |
| EDGE-002 | DM挙動は実際のDMチャンネル実行が必要 | ギルド外 `/ask` で拒否メッセージ確認 |
| EDGE-012 | 全角数字 + mention quick は実メンション経路が必要 | `@bot 明日１４時から１５時に打ち合わせ` の登録確認 |

## 自動継続可能（Copilot側で継続実施可能）

現在の未実施残件のうち、自動で継続できるものは 0件です。内訳は以下です。

| 区分 | 件数 | 代表的な内容 |
|---|---|---|
| UT | 0 | /ask 通常応答、履歴参照、検索、内部アクションの残件 |
| CT | 0 | /ask の複合シナリオ、文脈理解、DB実測シード |
| MT | 0 | @メンション同等の通常応答・調査・Reader系 |
| ERR | 0 | 未処理の異常系（エラーハンドリング） |
| CFG | 0 | 残る設定バリデーション |
| EDGE | 0 | 残る境界値ケース |

補足:
- QLP と SEC は、現在の未実施残件のうち手動必須または既に別扱いで整理済みです。
- 実行したものは必ず [docs/TEST_RESULTS.md](TEST_RESULTS.md) に反映してください。

## 運用ルール

- 手動実施した項目は、必ず [docs/TEST_RESULTS.md](TEST_RESULTS.md) の該当行を更新する。
- 新規に不具合を見つけた場合は、同ファイルの「不具合トラッカー」に追記する。
- Discordで実際に返ってきた本文のみを [docs/TEST_RESULTS.md](TEST_RESULTS.md) の §12 に追記する（要約文の転記は禁止）。

## BUG-004 / BUG-010 実機検証ランブック

### BUG-004（CT-004, CT-006）Discord callback 通知

1. 事前条件
	- `docker compose up -d` で Main/Research を起動。
	- `RESEARCH_NOTIFY_ON_COMPLETE=true` を有効化。
	- Discord 上で Bot が投稿可能な検証用チャンネルを用意。
2. CT-004 手順
	- 同チャンネルで `/deepdive kubernetes` を実行。
	- 即時応答で `queued` と `job_id` を確認。
	- 5分以内に同チャンネルへ完了通知が自動投稿されるか確認。
3. CT-006 手順
	- `DEEPDIVE_USE_RESEARCH_AGENT=true` で再実行。
	- polling 完了後に同チャンネル通知が投稿されるか確認。
4. クローズ条件
	- CT-004/006 の双方で「queued確認 + 同一チャンネル完了通知」を観測できること。
	- [docs/TEST_RESULTS.md](TEST_RESULTS.md) の CT-004/CT-006 を `PASS` に更新できること。

### BUG-010（CT-020）一般知識質問での履歴混入

1. 事前条件
	- 検証チャンネルに無関係履歴（例: `私は寿司が好き`）を保存しておく。
	- `DIRECTIONAL_MEMORY_ENABLED` などの境界設定は通常運用値で起動。
2. 手順
	- `/ask Python の標準ライブラリは？` を実行。
	- 返答に無関係履歴への依存表現がないことを確認。
	- 可能なら debug ログで `memory_context_policy=skip_history_context` を確認。
3. クローズ条件
	- 無関係履歴の混入が再現しないこと。
	- CT-020 を `PASS`（実機）に更新できること。
