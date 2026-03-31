from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from datetime import timezone
from pathlib import Path
from typing import Iterable

import discord
from discord import app_commands
from discord.ui import Button, View
from dotenv import load_dotenv

from discord_ai_agent.core.orchestrator import DiscordOrchestrator, load_orchestrator_config_from_env

MAX_TOTAL_INLINE = 15000
ATTACHMENT_NAME = "ask_response.txt"
CURSOR_FILE_NAME = "memory_ingest_cursor.json"


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


async def send_message_response(
    message: discord.Message,
    response_text: str,
    max_message_len: int,
) -> None:
    if len(response_text) > MAX_TOTAL_INLINE:
        summary = response_text[:700] + "\n\n(全文は添付ファイルを参照してください)"
        file_obj = discord.File(
            io.BytesIO(response_text.encode("utf-8")),
            filename=ATTACHMENT_NAME,
        )
        await message.reply(summary, file=file_obj, mention_author=False)
        return

    chunks = chunk_text(response_text, max_message_len)
    if not chunks:
        return
    await message.reply(chunks[0], mention_author=False)
    for chunk in chunks[1:]:
        await message.channel.send(chunk)


def ensure_runtime_dirs(paths: Iterable[str]) -> None:
    for path in paths:
        Path(path).mkdir(parents=True, exist_ok=True)


def _cursor_file_path(chromadb_path: str) -> Path:
    return Path(chromadb_path) / CURSOR_FILE_NAME


def load_ingest_cursor(chromadb_path: str) -> dict[str, int]:
    path = _cursor_file_path(chromadb_path)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.getLogger(__name__).exception("Failed to load ingest cursor file")
        return {}
    if not isinstance(raw, dict):
        return {}

    normalized: dict[str, int] = {}
    for key, value in raw.items():
        try:
            normalized[str(key)] = int(value)
        except Exception:
            continue
    return normalized


def save_ingest_cursor(chromadb_path: str, cursor_map: dict[str, int]) -> None:
    path = _cursor_file_path(chromadb_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cursor_map, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logging.getLogger(__name__).exception("Failed to save ingest cursor file")


def _cursor_key(guild_id: int | None, channel_id: int) -> str:
    return f"{guild_id or 0}:{channel_id}"


def iter_bootstrap_channels(guild: discord.Guild) -> list[discord.abc.MessageableChannel]:
    by_id: dict[int, discord.abc.MessageableChannel] = {}
    for channel in guild.text_channels:
        by_id[int(channel.id)] = channel
    for thread in guild.threads:
        by_id[int(thread.id)] = thread
    return list(by_id.values())


async def iter_archived_threads(
    guild: discord.Guild,
    include_private: bool,
    limit_per_parent: int,
) -> list[discord.Thread]:
    collected: dict[int, discord.Thread] = {}
    parent_candidates = list(guild.text_channels)
    parent_candidates.extend([c for c in guild.channels if isinstance(c, discord.ForumChannel)])

    history_limit = None if limit_per_parent <= 0 else limit_per_parent
    for parent in parent_candidates:
        if not hasattr(parent, "archived_threads"):
            continue

        for private_flag in ([False, True] if include_private else [False]):
            try:
                async for thread in parent.archived_threads(limit=history_limit, private=private_flag):
                    collected[int(thread.id)] = thread
            except Exception:
                logging.getLogger(__name__).debug(
                    "Skip archived thread scan: guild=%s parent=%s private=%s",
                    guild.id,
                    getattr(parent, "id", "unknown"),
                    private_flag,
                )

    return list(collected.values())


async def bootstrap_channel_history(
    orchestrator: DiscordOrchestrator,
    guild_id: int | None,
    channel: discord.abc.MessageableChannel,
    chromadb_path: str,
    cursor_map: dict[str, int],
    max_per_channel: int,
    batch_size: int,
    force_reindex: bool,
) -> int:
    channel_id = int(getattr(channel, "id", 0) or 0)
    if channel_id <= 0:
        return 0

    key = _cursor_key(guild_id, channel_id)
    after_id = None if force_reindex else cursor_map.get(key)
    after_obj = discord.Object(id=after_id) if after_id else None

    limit = None if max_per_channel <= 0 else max_per_channel
    payload: list[dict[str, int | str | bool]] = []
    ingested = 0
    latest_seen = after_id or 0

    try:
        async for msg in channel.history(limit=limit, oldest_first=True, after=after_obj):
            text = (msg.content or "").strip()
            if not text:
                continue
            payload.append(
                {
                    "message_id": int(msg.id),
                    "author_id": int(msg.author.id),
                    "is_bot": bool(getattr(msg.author, "bot", False)),
                    "content": text,
                    "created_at": msg.created_at.astimezone(timezone.utc).isoformat(),
                    "channel_name": str(getattr(msg.channel, "name", "") or ""),
                }
            )
            latest_seen = max(latest_seen, int(msg.id))

            if len(payload) >= batch_size:
                ingested += await orchestrator.ingest_channel_history(
                    guild_id=guild_id,
                    channel_id=channel_id,
                    messages=payload,
                )
                payload = []

        if payload:
            ingested += await orchestrator.ingest_channel_history(
                guild_id=guild_id,
                channel_id=channel_id,
                messages=payload,
            )
    except Exception:
        logging.getLogger(__name__).exception(
            "Failed bootstrap history: guild=%s channel=%s",
            guild_id,
            channel_id,
        )
        return ingested

    if latest_seen > (after_id or 0):
        cursor_map[key] = latest_seen
        save_ingest_cursor(chromadb_path, cursor_map)

    return ingested


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
    enable_message_content_intent = (
        os.getenv("DISCORD_ENABLE_MESSAGE_CONTENT_INTENT", "false").strip().lower() == "true"
    )
    bootstrap_on_ready = os.getenv("MEMORY_BOOTSTRAP_ON_READY", "true").strip().lower() == "true"
    bootstrap_max_per_channel = int(os.getenv("MEMORY_BOOTSTRAP_MAX_PER_CHANNEL", "0"))
    bootstrap_batch_size = int(os.getenv("MEMORY_BOOTSTRAP_BATCH_SIZE", "200"))
    bootstrap_force_reindex = os.getenv("MEMORY_BOOTSTRAP_FORCE_REINDEX", "false").strip().lower() == "true"
    bootstrap_include_archived = (
        os.getenv("MEMORY_BOOTSTRAP_INCLUDE_ARCHIVED_THREADS", "true").strip().lower() == "true"
    )
    bootstrap_archived_limit_per_parent = int(os.getenv("MEMORY_BOOTSTRAP_ARCHIVED_LIMIT_PER_PARENT", "0"))
    mention_ask_enabled = os.getenv("MENTION_ASK_ENABLED", "true").strip().lower() == "true"
    cli_approver_user_ids_raw = os.getenv("CLI_APPROVER_USER_IDS", "").strip()
    cli_approver_user_ids = {
        int(part.strip())
        for part in cli_approver_user_ids_raw.split(",")
        if part.strip().isdigit()
    }

    orchestrator_config = load_orchestrator_config_from_env()
    ensure_runtime_dirs(
        [
            orchestrator_config.chromadb_path,
            str(Path(orchestrator_config.profile_path).parent),
        ]
    )
    orchestrator = DiscordOrchestrator(orchestrator_config)
    ingest_cursor = load_ingest_cursor(orchestrator_config.chromadb_path)

    intents = discord.Intents.default()
    intents.message_content = enable_message_content_intent
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    class CliApprovalView(View):
        def __init__(
            self,
            command_text: str,
            requester_id: int,
            approver_ids: set[int],
        ) -> None:
            super().__init__(timeout=90)
            self.command_text = command_text
            self.requester_id = requester_id
            self.approver_ids = approver_ids

        def _is_approver(self, user_id: int) -> bool:
            if self.approver_ids:
                return user_id in self.approver_ids
            return user_id == self.requester_id

        @discord.ui.button(label="承認して実行", style=discord.ButtonStyle.success)
        async def approve(self, interaction: discord.Interaction, button: Button) -> None:  # type: ignore[override]
            if interaction.user is None or not self._is_approver(interaction.user.id):
                await interaction.response.send_message("この操作を承認できる権限がありません。", ephemeral=True)
                return

            approval_token = os.getenv("CLI_APPROVAL_TOKEN", "").strip()
            result = await asyncio.to_thread(
                orchestrator.tool_registry.execute,
                "run_local_cli",
                {"command": self.command_text, "approval_token": approval_token},
            )
            rendered = result if len(result) <= 1700 else result[:1700] + "..."
            self.disable_all_items()
            await interaction.response.edit_message(
                content=(
                    "CLI実行を承認しました。\n"
                    f"command: {self.command_text}\n\n"
                    f"```text\n{rendered}\n```"
                ),
                view=self,
            )

        @discord.ui.button(label="拒否", style=discord.ButtonStyle.danger)
        async def reject(self, interaction: discord.Interaction, button: Button) -> None:  # type: ignore[override]
            if interaction.user is None or not self._is_approver(interaction.user.id):
                await interaction.response.send_message("この操作を拒否できる権限がありません。", ephemeral=True)
                return

            self.disable_all_items()
            await interaction.response.edit_message(
                content=f"CLI実行は拒否されました。\ncommand: {self.command_text}",
                view=self,
            )

        async def on_timeout(self) -> None:
            self.disable_all_items()

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

    @tree.command(name="memory_status", description="メモリ保存状況を確認します（管理用）")
    async def memory_status(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.guild_id not in allowed_guild_ids:
            await interaction.response.send_message(
                "このサーバーではこのBotを利用できません。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            stats = await orchestrator.memory.get_guild_memory_stats(interaction.guild_id)
            lines = [
                f"guild: {stats.get('guild_id')}",
                f"collections: {stats.get('collection_count')}",
                f"total_records: {stats.get('total_records')}",
                "",
                "[top collections]",
            ]
            collections = stats.get("collections", [])
            top = sorted(collections, key=lambda x: int(x.get("count", 0)), reverse=True)[:12]
            for item in top:
                lines.append(f"- {item.get('name')}: {item.get('count')}")

            body = "\n".join(lines)
            chunks = chunk_text(body, 1800)
            for chunk in chunks:
                await interaction.followup.send(chunk, ephemeral=True)
        except Exception:
            logger.exception("Failed to handle /memory_status")
            await interaction.followup.send("メモリ状態の取得に失敗しました。", ephemeral=True)

    @tree.command(name="runcli", description="承認付きで許可済みCLIコマンドを実行します")
    @app_commands.describe(command="許可済みコマンドを入力（例: docker ps）")
    async def runcli(interaction: discord.Interaction, command: str) -> None:
        if interaction.guild_id is None or interaction.guild_id not in allowed_guild_ids:
            await interaction.response.send_message(
                "このサーバーではこのBotを利用できません。",
                ephemeral=True,
            )
            return

        clean_command = (command or "").strip()
        if not clean_command:
            await interaction.response.send_message("コマンドが空です。", ephemeral=True)
            return

        requester_id = interaction.user.id if interaction.user is not None else 0
        view = CliApprovalView(
            command_text=clean_command,
            requester_id=requester_id,
            approver_ids=cli_approver_user_ids,
        )
        await interaction.response.send_message(
            (
                "CLI実行リクエストを受け付けました。\n"
                f"command: {clean_command}\n"
                "承認者がボタンを押すと実行されます。"
            ),
            view=view,
            ephemeral=False,
        )

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

        if bootstrap_on_ready:
            if not enable_message_content_intent:
                logger.warning(
                    "Skip memory bootstrap because DISCORD_ENABLE_MESSAGE_CONTENT_INTENT=false. "
                    "Enable Message Content Intent in Discord Developer Portal and set env=true to use full history ingestion."
                )
                return

            total = 0
            for guild in client.guilds:
                if guild.id not in allowed_guild_ids:
                    continue

                targets: list[discord.abc.MessageableChannel] = list(iter_bootstrap_channels(guild))
                if bootstrap_include_archived:
                    archived_threads = await iter_archived_threads(
                        guild=guild,
                        include_private=False,
                        limit_per_parent=bootstrap_archived_limit_per_parent,
                    )
                    by_id = {int(getattr(ch, "id", 0)): ch for ch in targets}
                    for thread in archived_threads:
                        by_id[int(thread.id)] = thread
                    targets = [ch for _, ch in sorted(by_id.items(), key=lambda x: x[0])]

                for channel in targets:
                    count = await bootstrap_channel_history(
                        orchestrator=orchestrator,
                        guild_id=guild.id,
                        channel=channel,
                        chromadb_path=orchestrator_config.chromadb_path,
                        cursor_map=ingest_cursor,
                        max_per_channel=bootstrap_max_per_channel,
                        batch_size=bootstrap_batch_size,
                        force_reindex=bootstrap_force_reindex,
                    )
                    total += count
            logger.info("Memory bootstrap completed: ingested=%s", total)

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        if not enable_message_content_intent:
            return
        if message.guild is None:
            return
        if message.guild.id not in allowed_guild_ids:
            return

        content = (message.content or "").strip()
        if not content:
            return

        role = "assistant" if bool(getattr(message.author, "bot", False)) else "user"
        try:
            await orchestrator.memory.add_message(
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                role=role,
                content=content,
                user_id=message.author.id,
                message_id=message.id,
                metadata={
                    "source": "discord_stream",
                    "kind": "stream",
                    "timestamp": message.created_at.astimezone(timezone.utc).isoformat(),
                    "channel_name": str(getattr(message.channel, "name", "") or ""),
                },
            )
            key = _cursor_key(message.guild.id, message.channel.id)
            prev = ingest_cursor.get(key, 0)
            if message.id > prev:
                ingest_cursor[key] = int(message.id)
                save_ingest_cursor(orchestrator_config.chromadb_path, ingest_cursor)
        except Exception:
            logger.exception("Failed to ingest streaming message: guild=%s channel=%s", message.guild.id, message.channel.id)

        if not mention_ask_enabled:
            return
        if client.user is None:
            return
        if client.user not in message.mentions:
            return

        question = (message.content or "").replace(client.user.mention, "").strip()
        if not question:
            return

        try:
            answer = await orchestrator.answer(
                question=question,
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                user_id=message.author.id,
                message_id=message.id,
            )
            await send_message_response(message, answer, max_message_len=max_message_len)
        except Exception:
            logger.exception("Failed to handle mention ask: guild=%s channel=%s", message.guild.id, message.channel.id)
            try:
                await message.reply("応答中にエラーが発生しました。時間をおいて再試行してください。", mention_author=False)
            except Exception:
                logger.exception("Failed to send mention error message")

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
