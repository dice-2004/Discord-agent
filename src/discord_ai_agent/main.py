from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Iterable

import discord
from discord import app_commands
from dotenv import load_dotenv

from discord_ai_agent.core.orchestrator import DiscordOrchestrator, load_orchestrator_config_from_env

MAX_TOTAL_INLINE = 15000
ATTACHMENT_NAME = "ask_response.txt"


def setup_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper().strip()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def parse_allowed_guild_ids() -> set[int]:
    bot_guild_id_raw = os.getenv("BOT_GUILD_ID", "").strip()
    if not bot_guild_id_raw:
        raise ValueError("BOT_GUILD_ID is required")

    try:
        bot_guild_id = int(bot_guild_id_raw)
    except ValueError as exc:
        raise ValueError("BOT_GUILD_ID must be an integer") from exc

    allowed = {bot_guild_id}
    extra = os.getenv("ALLOWED_GUILD_IDS", "").strip()
    if extra:
        for part in extra.split(","):
            value = part.strip()
            if not value:
                continue
            try:
                allowed.add(int(value))
            except ValueError:
                logging.getLogger(__name__).warning("Ignore invalid guild id in ALLOWED_GUILD_IDS: %s", value)

    return allowed


def chunk_text(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        split_at = remaining.rfind("\n\n", 0, max_len)
        if split_at < max_len * 0.5:
            split_at = remaining.rfind("\n", 0, max_len)
        if split_at < max_len * 0.3:
            split_at = max_len

        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)
    return [chunk for chunk in chunks if chunk]


async def send_response(
    interaction: discord.Interaction,
    response_text: str,
    max_message_len: int,
) -> None:
    if len(response_text) > MAX_TOTAL_INLINE:
        summary = response_text[:700] + "\n\n(全文は添付ファイルを参照してください)"
        file_obj = discord.File(
            io.BytesIO(response_text.encode("utf-8")),
            filename=ATTACHMENT_NAME,
        )
        await interaction.followup.send(summary, file=file_obj)
        return

    for chunk in chunk_text(response_text, max_message_len):
        await interaction.followup.send(chunk)


def ensure_runtime_dirs(paths: Iterable[str]) -> None:
    for path in paths:
        Path(path).mkdir(parents=True, exist_ok=True)


def main() -> None:
    load_dotenv()
    setup_logging()
    logger = logging.getLogger(__name__)

    discord_token = os.getenv("DISCORD_TOKEN", "").strip()
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not discord_token or not gemini_api_key:
        raise RuntimeError("DISCORD_TOKEN and GEMINI_API_KEY are required")

    allowed_guild_ids = parse_allowed_guild_ids()
    max_message_len = int(os.getenv("MAX_DISCORD_MESSAGE_LEN", "1900"))

    orchestrator_config = load_orchestrator_config_from_env()
    ensure_runtime_dirs(
        [
            orchestrator_config.chromadb_path,
            str(Path(orchestrator_config.profile_path).parent),
        ]
    )
    orchestrator = DiscordOrchestrator(orchestrator_config)

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    @tree.command(name="ask", description="AIアシスタントに質問します")
    @app_commands.describe(question="質問内容")
    async def ask(interaction: discord.Interaction, question: str) -> None:
        logger.info("/ask received: guild=%s channel=%s", interaction.guild_id, interaction.channel_id)

        if interaction.guild_id is None or interaction.guild_id not in allowed_guild_ids:
            await interaction.response.send_message(
                "このサーバーではこのBotを利用できません。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            answer = await orchestrator.answer(
                question=question,
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                user_id=interaction.user.id,
                message_id=interaction.id,
            )
            await send_response(interaction, answer, max_message_len=max_message_len)
        except Exception:
            logger.exception("Failed to handle /ask")
            try:
                await interaction.followup.send("応答中にエラーが発生しました。時間をおいて再試行してください。")
            except Exception:
                logger.exception("Failed to send error message to Discord")

    @client.event
    async def on_ready() -> None:
        logger.info("Logged in as %s (%s)", client.user, client.user.id if client.user else "unknown")
        try:
            for guild_id in allowed_guild_ids:
                guild = discord.Object(id=guild_id)
                tree.copy_global_to(guild=guild)
                await tree.sync(guild=guild)
            logger.info("Command sync completed for guilds: %s", sorted(allowed_guild_ids))
        except Exception:
            logger.exception("Failed to sync commands")

    @tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        logger.exception("App command error: %s", error)
        if interaction.response.is_done():
            await interaction.followup.send("コマンド実行中にエラーが発生しました。")
        else:
            await interaction.response.send_message("コマンド実行中にエラーが発生しました。", ephemeral=True)

    try:
        client.run(discord_token, log_handler=None)
    except Exception:
        logger.exception("Discord client terminated unexpectedly")
        raise


if __name__ == "__main__":
    main()
