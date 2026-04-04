# Voice Jam + Voice Intelligence: Architecture Decision

## Goal
Discord VCでの音楽操作(Spotify Jam)と音声知能(文字起こし/意図判定)を、既存main-agentを詰まらせずに追加する。

## Decision Summary
- 優先制御は継続し、思考連続性は維持する。
- Discord接続はmain-agentのみが持ち、音声処理はHTTPで疎結合にする。
- コンテナは「新規1つ(voice-stt-agent統合) + 既存ollama利用」を採用する。

### Why thought continuity is preserved
優先制御は「実行中ジョブの割り込み」ではなく「開始順の制御」。
そのため、開始済みの research loop (gemini -> tool -> gemini ...) の連続性は壊れない。

## Recommended Runtime Topology
1. Existing: `main-agent`
2. Existing: `research-agent`
3. Existing: `ollama` (Gemma 4推論を継続利用)
4. New: `voice-stt-agent` (STT + intent + Spotify action)

### Responsibilities
- `voice-stt-agent`
  - main-agent から音声/文字起こしイベント受信
  - VAD/チャンク化
  - faster-whisperで文字起こし
  - transcriptの意図判定(Gemma 4 via Ollama)
  - Spotify API操作(キュー追加、検索、再生制御)
  - 天気連動選曲
  - 任意で音声チャンクのダンプ保存

- `ollama`
  - Gemma 4推論専用

## Why not 3 new containers (Gemma + STT + Broker)
N100/16GBでは、最初から3分割すると運用コスト/監視点/リソース圧迫が増えやすい。
既存ollamaを使えば、Gemma推論面は既に分離済みとみなせる。

## API Boundary (Minimal)
### main-agent -> voice-stt-agent
- `POST /v1/transcripts`
  - body:
    - `guild_id`: int
    - `channel_id`: int
    - `user_id`: int
    - `text`: string
    - `started_at`: iso8601
    - `ended_at`: iso8601

- `POST /v1/audio/chunks`
  - headers:
    - `X-Guild-Id`
    - `X-Channel-Id`
    - `X-User-Id`
    - `X-Audio-Ext` (wav/pcm 等)
  - body:
    - audio bytes

### health
- `GET /healthz` on `voice-stt-agent`

## Event Flow (No main-agent path)
1. User speaks in VC
2. `main-agent` が音声チャンクを `voice-stt-agent` へ送信
3. `voice-stt-agent` が文字起こし
4. `voice-stt-agent` が Ollama(Gemma 4) で意図判定
5. if music intent -> Spotify API call
6. if memo intent -> optional save to memory endpoint (future)
7. result messageを main-agent が必要に応じて Discordへ返答

## Reliability Controls
- `voice-stt-agent`
  - bounded queue (drop-oldest)
  - reconnect backoff for voice gateway
  - max utterance length guard

- `voice-stt-agent`
  - idempotency key per utterance
  - Spotify 429 retry with jitter (phase 2)
  - fallback response when Gemma timeout

## Security
- Internal shared token header for service-to-service calls
- Spotify OAuth refresh token only in `voice-stt-agent`
- VC transcript retention TTL configurable

## Stepwise Rollout
1. Phase 1: transcript -> intent -> log only (no Spotify write)
2. Phase 2: Spotify read/search only
3. Phase 3: Jam queue write enable
4. Phase 4: weather auto-add + policy tuning

## Compose Add-on Plan
`docker-compose.yml`では `voice-stt-agent` のみを voice profile に追加。
`voice-stt-agent` は `ollama` にHTTP接続。
`main-agent` は `voice-stt-agent` にのみ送信する。

## Open Questions
- transcript保存をmain-agent memoryへ統合するか、別DBにするか
- 日本語固有名詞のSTT誤認識補正辞書をどこで管理するか
