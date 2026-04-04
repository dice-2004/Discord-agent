from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import discord

from voice_stt_agent.server import _forward_transcript, _safe_int, start_http_server_in_thread

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_transcript_event(
    *,
    guild_id: int,
    channel_id: int,
    user_id: int,
    text: str,
    source: str,
) -> dict[str, Any]:
    now = _now_iso()
    return {
        "guild_id": guild_id,
        "channel_id": channel_id,
        "user_id": user_id,
        "text": text,
        "started_at": now,
        "ended_at": now,
        "source": source,
        "created_at": now,
    }


class VoiceSttDiscordClient(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        @self.tree.command(name="vc_join", description="実行者がいるVCへ voice-stt-agent を参加させる")
        async def vc_join(interaction: discord.Interaction) -> None:
            if interaction.guild is None or interaction.user is None:
                await interaction.response.send_message("Guild内で実行してください。", ephemeral=True)
                return
            member = interaction.guild.get_member(interaction.user.id)
            voice_state = member.voice if member is not None else None
            if voice_state is None or voice_state.channel is None:
                await interaction.response.send_message("先にVCへ参加してください。", ephemeral=True)
                return

            target = voice_state.channel
            existing = discord.utils.get(self.voice_clients, guild=interaction.guild)
            if existing is not None and existing.channel is not None and existing.channel.id == target.id:
                await interaction.response.send_message(f"既に {target.name} に参加しています。", ephemeral=True)
                return

            if existing is not None:
                await existing.move_to(target)
            else:
                await target.connect(self_deaf=True)
            await interaction.response.send_message(f"VC `{target.name}` に参加しました。", ephemeral=True)

        @self.tree.command(name="vc_leave", description="voice-stt-agent をVCから退出させる")
        async def vc_leave(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await interaction.response.send_message("Guild内で実行してください。", ephemeral=True)
                return
            existing = discord.utils.get(self.voice_clients, guild=interaction.guild)
            if existing is None:
                await interaction.response.send_message("VCに参加していません。", ephemeral=True)
                return
            await existing.disconnect(force=True)
            await interaction.response.send_message("VCから退出しました。", ephemeral=True)

        @self.tree.command(name="vc_status", description="voice-stt-agent のVC参加状態を表示")
        async def vc_status(interaction: discord.Interaction) -> None:
            if interaction.guild is None:
                await interaction.response.send_message("Guild内で実行してください。", ephemeral=True)
                return
            existing = discord.utils.get(self.voice_clients, guild=interaction.guild)
            if existing is None or existing.channel is None:
                await interaction.response.send_message("VC未参加です。", ephemeral=True)
                return
            await interaction.response.send_message(
                f"参加中: `{existing.channel.name}` (guild={interaction.guild.id})",
                ephemeral=True,
            )

        @self.tree.command(name="vc_transcript_mock", description="検証用: 文をvoice-stt-agentへ送信")
        @discord.app_commands.describe(text="文字起こし結果として扱うテキスト")
        async def vc_transcript_mock(interaction: discord.Interaction, text: str) -> None:
            if interaction.guild is None or interaction.channel is None or interaction.user is None:
                await interaction.response.send_message("Guild内で実行してください。", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            event = _build_transcript_event(
                guild_id=int(interaction.guild.id),
                channel_id=int(interaction.channel.id),
                user_id=int(interaction.user.id),
                text=(text or "").strip(),
                source="slash_mock",
            )
            status, err = _forward_transcript(event)
            if err is not None:
                await interaction.followup.send(
                    f"転送失敗: status={status} detail={err[:300]}",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                f"転送成功: forward_status={status} text={event['text'][:120]}",
                ephemeral=True,
            )

        guild_ids_raw = os.getenv("VOICE_BOT_GUILD_IDS", "").strip()
        if guild_ids_raw:
            synced = 0
            for token in guild_ids_raw.split(","):
                token = token.strip()
                if not token:
                    continue
                try:
                    gid = int(token)
                except ValueError:
                    continue
                guild_obj = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild_obj)
                await self.tree.sync(guild=guild_obj)
                synced += 1
            logger.info("voice-stt slash sync (guild) completed: %s", synced)
        else:
            await self.tree.sync()
            logger.info("voice-stt slash sync (global) completed")

    async def on_ready(self) -> None:
        logger.info("Voice STT Discord logged in as %s (%s)", self.user, getattr(self.user, "id", 0))


async def _run_discord_bot() -> None:
    token = os.getenv("VOICE_DISCORD_TOKEN", "").strip() or os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("VOICE_DISCORD_TOKEN or DISCORD_TOKEN is required when VOICE_STT_ENABLE_DISCORD=true")
    client = VoiceSttDiscordClient()
    await client.start(token)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, (os.getenv("LOG_LEVEL", "INFO").upper().strip() or "INFO"), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    host = os.getenv("VOICE_STT_HOST", "0.0.0.0").strip() or "0.0.0.0"
    port = _safe_int("VOICE_STT_PORT", 8095)
    start_http_server_in_thread(host=host, port=port)

    enable_discord = os.getenv("VOICE_STT_ENABLE_DISCORD", "true").strip().lower() == "true"
    if not enable_discord:
        logger.info("VOICE_STT_ENABLE_DISCORD=false -> HTTP endpoint only mode")
        asyncio.get_event_loop().run_forever()
        return

    asyncio.run(_run_discord_bot())


if __name__ == "__main__":
    main()
