Discord AI-Assistant: Spotify Jam & Voice Intelligence 構成案

確定版アーキテクチャは以下に保存:
- docs/VOICE_JAM_ARCHITECTURE.md
1. アイデア概要 (Concept)

本プロジェクトは、Discordボイスチャンネル（VC）における「音楽セッションの自動化」と「会話の知能化」を両立させる。Bot自身は音声を流さず、ユーザー全員が参加する Spotify Jam の「AIコントローラー」として機能する。同時に、VC内の音声をリアルタイムでテキスト化し、既存のメッセージ履歴保存機能（ChannelMemoryStore）と連携させることで、高度な文脈理解と議事録作成を実現する。
2. 機能詳細 (Functional Requirements)
A. Spotify Jam 連携（音楽操作）

    Jamへの動的追加: ユーザーがVCで「次は〇〇を流して」と言う、あるいはチャットで指示すると、AIが曲を特定し、Spotify APIを通じてJamのプレイリスト（キュー）に直接追加する。

    再生管理: 音は各ユーザーのSpotifyアプリから流れるため、Botは再生処理（Lavalink等）を行わず、APIによる「キュー操作」のみを担当する。

    環境連動選曲: OpenWeatherMap APIから取得した天気情報に基づき、AIがJamにふさわしい曲を自律的に選定して追加する。

B. 音声知能・会議アシスタント（実用機能）

    リアルタイム文字起こし: VC内の会話を常時取得し、faster-whisper でテキスト化する。

    会話履歴の蓄積: テキスト化された会話を、既存の orchestrator.py のロジックを用いて MemoryRecord として保存する。

    文脈理解と要約: 「さっきの話を要約して」といったリクエストに対し、保存された音声ログから内容を抽出して回答する。

3. 使用技術 (Technical Stack)
インフラ・AI推論

    ハードウェア: N100 PC (RAM 16GB) / Docker環境。

    STTエンジン: faster-whisper (base)。N100のCPUで動作。

    LLM:

        Gemini 2.5 Flash: 音声からの意図抽出、高度な要約に使用。

        Gemma 4 (Local/Ollama): プライバシーを要する社内会議の処理や、シンプルなタスク管理に使用。

API・ライブラリ

    Discord SDK: discord.py + discord-ext-voice-recv (DAVE/E2EE対応版)。

    Music: Spotify Web API (spotipy)。user-modify-playback-state 権限を利用。

    Weather: OpenWeatherMap API。

4. 既存コードへの統合プラン (Implementation Strategy)
1. ツール群の拡張

    src/tools/music_tools.py を新規作成。

        add_to_jam(track_name): Spotifyで検索し、アクティブなJamセッションのキューに追加する関数を実装。

    src/tools/action_tools.py の execute_internal_action に音楽操作用のエントリーポイントを追加する。

2. オーケストレーターの調整

    orchestrator.py の _answer_impl を拡張し、音声入力から生成されたテキストを処理対象に含める。

    system_prompt に「あなたはSpotify JamのDJであり、会議の書記でもある」という指示を追加する。

3. 音声処理パイプライン

    main.py に VoiceClient のイベントハンドラを実装し、受信した音声データを faster-whisper へ流し込む非同期ループを作成する。

    文字起こし結果が「特定のコマンド」と判定された場合は execute_tool_job を介してSpotify操作を実行し、それ以外は memory.add_message でログとして保存する。
