from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import discord
from discord import app_commands
from discord.ui import Button, View
from dotenv import load_dotenv

from main_agent.core.orchestrator import DiscordOrchestrator, load_orchestrator_config_from_env

MAX_TOTAL_INLINE = 15000
ATTACHMENT_NAME = "ask_response.txt"
RESEARCH_ATTACHMENT_NAME = "research_report.txt"
CURSOR_FILE_NAME = "memory_ingest_cursor.json"
RUNCLI_AUDIT_LOG_DEFAULT = "./data/audit/runcli_audit.jsonl"
RESEARCH_AUDIT_LOG_DEFAULT = "./data/audit/research_audit.jsonl"


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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_runcli_audit_log_path() -> Path:
    raw = os.getenv("RUNCLI_AUDIT_LOG_PATH", RUNCLI_AUDIT_LOG_DEFAULT).strip()
    path = Path(raw or RUNCLI_AUDIT_LOG_DEFAULT)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def resolve_research_audit_log_path() -> Path:
    raw = os.getenv("RESEARCH_AUDIT_LOG_PATH", RESEARCH_AUDIT_LOG_DEFAULT).strip()
    path = Path(raw or RESEARCH_AUDIT_LOG_DEFAULT)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def append_research_audit(path: Path, payload: dict[str, object]) -> None:
    logger = logging.getLogger(__name__)
    row = {"ts": _utc_now_iso(), **payload}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("Failed to append research audit log")


def append_runcli_audit(path: Path, payload: dict[str, object]) -> None:
    logger = logging.getLogger(__name__)
    row = {"ts": _utc_now_iso(), **payload}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("Failed to append runcli audit log")


def read_runcli_audit_tail(path: Path, limit: int) -> list[dict[str, object]]:
    if limit <= 0:
        return []
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8") as fp:
            lines = fp.readlines()
    except Exception:
        logging.getLogger(__name__).exception("Failed to read runcli audit log")
        return []

    rows: list[dict[str, object]] = []
    for row in lines[-limit:]:
        raw = row.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _tokenize_for_logsearch(text: str) -> set[str]:
    if not text:
        return set()
    return set(re.findall(r"[a-zA-Z0-9_\-]+|[一-龥]{2,}|[ぁ-ん]{2,}|[ァ-ンー]{2,}", text.lower()))


def _logsearch_overlap_score(keyword: str, content: str) -> float:
    q_tokens = _tokenize_for_logsearch(keyword)
    if not q_tokens:
        return 0.0
    c_tokens = _tokenize_for_logsearch(content)
    if not c_tokens:
        return 0.0
    hit = len(q_tokens.intersection(c_tokens))
    return hit / max(len(q_tokens), 1)


def _logsearch_recency_score(timestamp_text: str) -> float:
    if not timestamp_text:
        return 0.15
    try:
        parsed = datetime.fromisoformat(str(timestamp_text).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days = max((now - parsed.astimezone(timezone.utc)).total_seconds() / 86400.0, 0.0)
        if days <= 1:
            return 1.0
        if days <= 7:
            return 0.82
        if days <= 30:
            return 0.56
        if days <= 90:
            return 0.34
        return 0.18
    except Exception:
        return 0.15


def _safe_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError:
        return default


def _extract_time_range(text: str) -> tuple[int, int, int, int] | None:
    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*[-~〜]\s*(\d{1,2})(?::(\d{2}))?", text)
    if not match:
        return None
    sh = int(match.group(1))
    sm = int(match.group(2) or "0")
    eh = int(match.group(3))
    em = int(match.group(4) or "0")
    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        return None
    return sh, sm, eh, em


def _extract_date_base(text: str, now_jst: datetime) -> datetime | None:
    jp_date_match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if jp_date_match:
        try:
            y = int(jp_date_match.group(1))
            m = int(jp_date_match.group(2))
            d = int(jp_date_match.group(3))
            return datetime(y, m, d, tzinfo=now_jst.tzinfo)
        except Exception:
            return None

    date_match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if date_match:
        try:
            y = int(date_match.group(1))
            m = int(date_match.group(2))
            d = int(date_match.group(3))
            return datetime(y, m, d, tzinfo=now_jst.tzinfo)
        except Exception:
            return None

    md_match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if md_match:
        try:
            m = int(md_match.group(1))
            d = int(md_match.group(2))
            base = datetime(now_jst.year, m, d, tzinfo=now_jst.tzinfo)
            # Past date in current year is treated as next year for natural scheduling intent.
            if base.date() < now_jst.date() - timedelta(days=1):
                base = base.replace(year=base.year + 1)
            return base
        except Exception:
            return None
    if "明日" in text:
        return (now_jst + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if "今日" in text:
        return now_jst.replace(hour=0, minute=0, second=0, microsecond=0)
    return None


def _extract_title(text: str) -> str:
    # Pattern: "タイトル: D月D日" or "D月D日: タイトル"
    # Try format 1: タイトル: D月D日（終日）
    pattern1 = re.search(r"^([^:：\d\n]+?)\s*[:：]\s*\d{1,2}\s*月\s*\d{1,2}\s*日", text)
    if pattern1:
        title = pattern1.group(1).strip()
        if title:
            return title

    # Try format 2: D月D日: タイトル（終日）
    pattern2 = re.search(r"\d{1,2}\s*月\s*\d{1,2}\s*日\s*[:：]\s*([^\n]+)", text)
    if pattern2:
        raw = pattern2.group(1).strip()
        raw = re.sub(r"\s*[（(]\s*終日\s*[)）]\s*", " ", raw).strip()
        raw = re.sub(r"\s*(?:を)?(?:登録|追加|入れて|作成)(?:して|してください|して下さい)?\s*$", "", raw).strip()
        if raw:
            return raw

    # Try quoted format: 「タイトル」
    quoted = re.search(r"[「\"]([^\"」]+)[」\"]", text)
    if quoted:
        return quoted.group(1).strip()

    # Try key format: 内容: X / タイトルは X
    key_match = re.search(r"(?:内容|件名|タイトル)\s*(?:[:：]|は)\s*([^\n]+)", text)
    if key_match:
        return key_match.group(1).strip()

    # Try task phrasing: "タスクリストへ 面接準備 を追加して"
    task_list_match = re.search(
        r"(?:タスクリスト|やること(?:リスト)?|todo|to\s*do|to-do)\s*(?:へ|に|として)?\s*([^\n]+?)\s*(?:を)?(?:追加|登録|入れ|作成)",
        text,
        flags=re.IGNORECASE,
    )
    if task_list_match:
        raw = task_list_match.group(1).strip()
        raw = re.sub(r"^(?:タイトル|件名)\s*(?:[:：]|は)\s*", "", raw).strip()
        if raw:
            return raw

    # Try task phrasing: "やることとして課題を追加して"
    task_as_match = re.search(r"(?:として)\s*([^\n]+?)\s*(?:を)?(?:追加|登録|入れ|作成)", text)
    if task_as_match:
        raw = task_as_match.group(1).strip()
        raw = re.sub(r"^(?:タイトル|件名)\s*(?:[:：]|は)\s*", "", raw).strip()
        if raw:
            return raw

    # Try command-tail format: "...追加して <title>"
    tail_match = re.search(r"(?:追加して|登録して|入れて|作成して)\s*([^\n]+)$", text)
    if tail_match:
        raw = tail_match.group(1).strip()
        raw = re.sub(r"^(?:タイトル|件名)\s*(?:[:：]|は)\s*", "", raw).strip()
        raw = re.sub(r"\s*[（(]\s*終日\s*[)）]\s*$", "", raw).strip()
        if raw:
            return raw

    return "予定"


def _is_task_intent(text: str) -> bool:
    lowered = text.lower()
    direct_keywords = (
        "タスク",
        "タスクリスト",
        "todo",
        "to do",
        "to-do",
        "やること",
        "やることリスト",
        "チェックリスト",
    )
    if any(key in lowered for key in direct_keywords):
        return True

    # 「課題」単独はタスク確定にしない。文脈でタスク管理意図があるときだけtrue。
    if "課題" in text:
        contextual_patterns = [
            r"課題.*(?:として|で).*(?:管理|記録|整理)",
            r"(?:管理|記録|整理).*(?:課題)",
            r"(?:やること|todo|to do|to-do).*(?:課題)",
        ]
        for pattern in contextual_patterns:
            if re.search(pattern, lowered):
                return True

    return False


def build_quick_calendar_action(question: str) -> tuple[str, dict[str, object]] | None:
    text = (question or "").strip()
    if not text:
        return None

    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst)

    # Quick retrieval intents.
    if "予定" in text and any(k in text for k in ("今月", "来月", "今日", "明日", "今週")) and "追加" not in text:
        if "今月" in text:
            start = now_jst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if start.month == 12:
                end = start.replace(year=start.year + 1, month=1)
            else:
                end = start.replace(month=start.month + 1)
        elif "来月" in text:
            this_month = now_jst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if this_month.month == 12:
                start = this_month.replace(year=this_month.year + 1, month=1)
            else:
                start = this_month.replace(month=this_month.month + 1)
            if start.month == 12:
                end = start.replace(year=start.year + 1, month=1)
            else:
                end = start.replace(month=start.month + 1)
        elif "今週" in text:
            start = (now_jst - timedelta(days=now_jst.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=7)
        elif "明日" in text:
            start = (now_jst + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
        else:
            start = now_jst.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)

        return "get_calendar_events", {
            "time_min": start.isoformat(),
            "time_max": end.isoformat(),
        }

    # Quick add intents.
    add_intent = any(
        k in text for k in ("予定追加", "追加して", "登録して", "カレンダーに", "入れて", "タスク", "todo", "ToDo", "やること")
    )
    is_task = _is_task_intent(text)
    if not add_intent:
        add_intent = (
            ("内容" in text and "日時" in text)
            and bool(re.search(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[/-]\d{1,2}[/-]\d{1,2}", text))
        )

    if add_intent:
        date_base = _extract_date_base(text, now_jst)
        time_range = _extract_time_range(text)
        all_day = any(k in text for k in ("終日", "全日", "一日中", "1日中"))
        if time_range is not None:
            sh, sm, eh, em = time_range
            if sh == 0 and sm == 0 and eh == 23 and em in {59, 60}:
                all_day = True
        if date_base is None:
            return None

        # Task intent detected -> add_task action
        if is_task:
            return "add_task", {
                "title": _extract_title(text),
                "due_date": date_base.strftime("%Y-%m-%d"),
            }

        # Calendar event intent
        if time_range is None and not all_day:
            return None

        if all_day:
            return "add_calendar_event", {
                "title": _extract_title(text),
                "all_day": True,
                "date": date_base.strftime("%Y-%m-%d"),
            }

        if time_range is None:
            return None
        sh, sm, eh, em = time_range
        start = date_base.replace(hour=sh, minute=sm)
        end = date_base.replace(hour=eh, minute=em)
        if end <= start:
            end = end + timedelta(days=1)

        return "add_calendar_event", {
            "title": _extract_title(text),
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
        }

    return None


def _parse_int_set_env(name: str) -> set[int]:
    raw = os.getenv(name, "").strip()
    values: set[int] = set()
    if not raw:
        return values
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError:
            logging.getLogger(__name__).warning("Ignore invalid integer in %s: %s", name, item)
    return values


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
    mention_require_prefix = os.getenv("MENTION_REQUIRE_PREFIX", "true").strip().lower() == "true"
    mention_quick_calendar_enabled = os.getenv("MENTION_QUICK_CALENDAR_ENABLED", "true").strip().lower() == "true"
    deepdive_use_research_agent = os.getenv("DEEPDIVE_USE_RESEARCH_AGENT", "true").strip().lower() == "true"
    research_notify_on_complete = os.getenv("RESEARCH_NOTIFY_ON_COMPLETE", "true").strip().lower() == "true"
    research_notify_timeout_sec = int(os.getenv("RESEARCH_NOTIFY_TIMEOUT_SEC", "600"))
    research_notify_poll_sec = int(os.getenv("RESEARCH_NOTIFY_POLL_SEC", "3"))
    if research_notify_timeout_sec < 30:
        research_notify_timeout_sec = 30
    if research_notify_poll_sec < 1:
        research_notify_poll_sec = 1
    logsearch_default_scope = os.getenv("LOGSEARCH_DEFAULT_SCOPE", "guild").strip().lower()
    if logsearch_default_scope not in {"channel", "guild"}:
        logsearch_default_scope = "guild"
    cli_approver_user_ids_raw = os.getenv("CLI_APPROVER_USER_IDS", "").strip()
    runcli_audit_tail_default = int(os.getenv("RUNCLI_AUDIT_TAIL_DEFAULT", "20"))
    if runcli_audit_tail_default < 1:
        runcli_audit_tail_default = 20
    runcli_audit_event_filter_default = os.getenv("RUNCLI_AUDIT_EVENT_FILTER_DEFAULT", "all").strip().lower()
    if not runcli_audit_event_filter_default:
        runcli_audit_event_filter_default = "all"
    logsearch_include_score = os.getenv("LOGSEARCH_INCLUDE_SCORE", "true").strip().lower() == "true"
    score_overlap_weight = _safe_float_env("LOGSEARCH_SCORE_OVERLAP_WEIGHT", 0.7)
    score_recency_weight = _safe_float_env("LOGSEARCH_SCORE_RECENCY_WEIGHT", 0.3)
    if score_overlap_weight < 0:
        score_overlap_weight = 0.0
    if score_recency_weight < 0:
        score_recency_weight = 0.0
    weight_sum = score_overlap_weight + score_recency_weight
    if weight_sum <= 0:
        score_overlap_weight, score_recency_weight = 0.7, 0.3
    else:
        score_overlap_weight /= weight_sum
        score_recency_weight /= weight_sum
    persona_memory_enabled = os.getenv("PERSONA_MEMORY_ENABLED", "true").strip().lower() == "true"
    directional_memory_enabled = os.getenv("DIRECTIONAL_MEMORY_ENABLED", "false").strip().lower() == "true"
    personal_guild_id = int(os.getenv("PERSONAL_GUILD_ID", "0") or 0)
    family_guild_ids = _parse_int_set_env("FAMILY_GUILD_IDS")
    cli_approver_user_ids = {
        int(part.strip())
        for part in cli_approver_user_ids_raw.split(",")
        if part.strip().isdigit()
    }

    orchestrator_config = load_orchestrator_config_from_env()
    runcli_audit_log_path = resolve_runcli_audit_log_path()
    research_audit_log_path = resolve_research_audit_log_path()
    ensure_runtime_dirs(
        [
            orchestrator_config.chromadb_path,
            str(Path(orchestrator_config.profile_path).parent),
            str(runcli_audit_log_path.parent),
            str(research_audit_log_path.parent),
        ]
    )
    orchestrator = DiscordOrchestrator(orchestrator_config)
    orchestrator.configure_directional_memory_policy(
        enabled=directional_memory_enabled,
        personal_guild_id=personal_guild_id if personal_guild_id > 0 else None,
        family_guild_ids=family_guild_ids,
    )
    ingest_cursor = load_ingest_cursor(orchestrator_config.chromadb_path)

    intents = discord.Intents.default()
    intents.message_content = enable_message_content_intent
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    research_notify_tasks: set[asyncio.Task[None]] = set()

    async def _resolve_channel(channel_id: int) -> discord.abc.Messageable | None:
        channel = client.get_channel(channel_id)
        if channel is not None:
            return channel  # type: ignore[return-value]
        try:
            fetched = await client.fetch_channel(channel_id)
            return fetched  # type: ignore[return-value]
        except Exception:
            logger.exception("Failed to resolve channel: channel_id=%s", channel_id)
            return None

    async def _start_research_notification(
        *,
        job_id: str,
        topic: str,
        source: str,
        channel_id: int,
    ) -> None:
        async def _runner() -> None:
            started = datetime.now(timezone.utc)
            await orchestrator.save_workflow_checkpoint(
                workflow="research_job",
                status="queued",
                payload={
                    "job_id": job_id,
                    "topic": topic,
                    "source": source,
                    "channel_id": channel_id,
                    "started_at": started.isoformat(),
                },
                job_id=job_id,
            )
            append_research_audit(
                research_audit_log_path,
                {
                    "event": "queued",
                    "job_id": job_id,
                    "topic": topic,
                    "source": source,
                    "channel_id": channel_id,
                },
            )

            elapsed = 0
            while elapsed <= research_notify_timeout_sec:
                try:
                    raw = await orchestrator.execute_tool_job(
                        tool_name="get_research_job_status",
                        args={"job_id": job_id},
                        task_label=f"research_status:{job_id}",
                    )
                    status_payload = json.loads(raw)
                    if not isinstance(status_payload, dict):
                        status_payload = {"status": "error", "detail": str(status_payload)}
                except Exception as exc:
                    logger.exception("Failed polling research job status: job_id=%s", job_id)
                    append_research_audit(
                        research_audit_log_path,
                        {
                            "event": "poll_failed",
                            "job_id": job_id,
                            "topic": topic,
                            "source": source,
                            "channel_id": channel_id,
                            "error": str(exc)[:600],
                        },
                    )
                    await orchestrator.save_workflow_checkpoint(
                        workflow="research_job",
                        status="failed",
                        payload={
                            "job_id": job_id,
                            "topic": topic,
                            "source": source,
                            "channel_id": channel_id,
                            "error": str(exc)[:600],
                        },
                        job_id=job_id,
                    )
                    return

                status = str(status_payload.get("status", "")).strip().lower()
                if status == "done":
                    report = str(status_payload.get("report", "")).strip() or "(レポート本文なし)"
                    channel = await _resolve_channel(channel_id)
                    if channel is not None:
                        summary = (
                            "Research Agent の調査が完了しました。\n"
                            f"job_id: {job_id}\n"
                            f"topic: {topic}\n"
                            f"source: {source}"
                        )
                        if len(report) > MAX_TOTAL_INLINE:
                            file_obj = discord.File(
                                io.BytesIO(report.encode("utf-8")),
                                filename=RESEARCH_ATTACHMENT_NAME,
                            )
                            await channel.send(summary + "\n\n(全文は添付ファイルを参照してください)", file=file_obj)
                        else:
                            await channel.send(summary)
                            for chunk in chunk_text(report, max_message_len):
                                await channel.send(chunk)

                    append_research_audit(
                        research_audit_log_path,
                        {
                            "event": "done",
                            "job_id": job_id,
                            "topic": topic,
                            "source": source,
                            "channel_id": channel_id,
                            "report_chars": len(report),
                        },
                    )

                    await orchestrator.save_workflow_checkpoint(
                        workflow="research_job",
                        status="done",
                        payload={
                            "job_id": job_id,
                            "topic": topic,
                            "source": source,
                            "channel_id": channel_id,
                            "finished_at": datetime.now(timezone.utc).isoformat(),
                        },
                        job_id=job_id,
                    )
                    return

                if status == "failed":
                    detail = str(status_payload.get("error") or status_payload.get("detail") or "unknown")
                    channel = await _resolve_channel(channel_id)
                    if channel is not None:
                        await channel.send(
                            "Research Agent の調査が失敗しました。\n"
                            f"job_id: {job_id}\n"
                            f"topic: {topic}\n"
                            f"detail: {detail[:700]}"
                        )
                    append_research_audit(
                        research_audit_log_path,
                        {
                            "event": "failed",
                            "job_id": job_id,
                            "topic": topic,
                            "source": source,
                            "channel_id": channel_id,
                            "error": detail[:600],
                        },
                    )
                    await orchestrator.save_workflow_checkpoint(
                        workflow="research_job",
                        status="failed",
                        payload={
                            "job_id": job_id,
                            "topic": topic,
                            "source": source,
                            "channel_id": channel_id,
                            "error": detail[:600],
                        },
                        job_id=job_id,
                    )
                    return

                await orchestrator.save_workflow_checkpoint(
                    workflow="research_job",
                    status=status or "running",
                    payload={
                        "job_id": job_id,
                        "topic": topic,
                        "source": source,
                        "channel_id": channel_id,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    job_id=job_id,
                )

                await asyncio.sleep(research_notify_poll_sec)
                elapsed = int((datetime.now(timezone.utc) - started).total_seconds())

            channel = await _resolve_channel(channel_id)
            if channel is not None:
                await channel.send(
                    "Research Agent の調査が長時間実行中です。\n"
                    f"job_id: {job_id}\n"
                    "しばらくしてから再確認します。"
                )
            append_research_audit(
                research_audit_log_path,
                {
                    "event": "timeout",
                    "job_id": job_id,
                    "topic": topic,
                    "source": source,
                    "channel_id": channel_id,
                },
            )

        task = asyncio.create_task(_runner(), name=f"research-notify-{job_id}")
        research_notify_tasks.add(task)
        task.add_done_callback(lambda t: research_notify_tasks.discard(t))

    class CliApprovalView(View):
        def __init__(
            self,
            command_text: str,
            requester_id: int,
            approver_ids: set[int],
            guild_id: int,
            channel_id: int,
            audit_log_path: Path,
        ) -> None:
            super().__init__(timeout=90)
            self.command_text = command_text
            self.requester_id = requester_id
            self.approver_ids = approver_ids
            self.guild_id = guild_id
            self.channel_id = channel_id
            self.audit_log_path = audit_log_path

        def _is_approver(self, user_id: int) -> bool:
            if self.approver_ids:
                return user_id in self.approver_ids
            return user_id == self.requester_id

        @discord.ui.button(label="承認して実行", style=discord.ButtonStyle.success)
        async def approve(self, interaction: discord.Interaction, button: Button) -> None:  # type: ignore[override]
            if interaction.user is None or not self._is_approver(interaction.user.id):
                append_runcli_audit(
                    self.audit_log_path,
                    {
                        "event": "unauthorized_approve",
                        "guild_id": self.guild_id,
                        "channel_id": self.channel_id,
                        "requester_id": self.requester_id,
                        "actor_id": interaction.user.id if interaction.user else 0,
                        "command": self.command_text,
                    },
                )
                await interaction.response.send_message("この操作を承認できる権限がありません。", ephemeral=True)
                return

            append_runcli_audit(
                self.audit_log_path,
                {
                    "event": "approved",
                    "guild_id": self.guild_id,
                    "channel_id": self.channel_id,
                    "requester_id": self.requester_id,
                    "actor_id": interaction.user.id,
                    "command": self.command_text,
                },
            )

            approval_token = os.getenv("CLI_APPROVAL_TOKEN", "").strip()
            result = await asyncio.to_thread(
                orchestrator.tool_registry.execute,
                "run_local_cli",
                {"command": self.command_text, "approval_token": approval_token},
            )
            exit_code: int | None = None
            if result.startswith("[exit="):
                try:
                    exit_code = int(result.split("]", 1)[0].replace("[exit=", ""))
                except Exception:
                    exit_code = None

            append_runcli_audit(
                self.audit_log_path,
                {
                    "event": "executed",
                    "guild_id": self.guild_id,
                    "channel_id": self.channel_id,
                    "requester_id": self.requester_id,
                    "actor_id": interaction.user.id,
                    "command": self.command_text,
                    "exit_code": exit_code,
                    "result_preview": result[:280],
                },
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
                append_runcli_audit(
                    self.audit_log_path,
                    {
                        "event": "unauthorized_reject",
                        "guild_id": self.guild_id,
                        "channel_id": self.channel_id,
                        "requester_id": self.requester_id,
                        "actor_id": interaction.user.id if interaction.user else 0,
                        "command": self.command_text,
                    },
                )
                await interaction.response.send_message("この操作を拒否できる権限がありません。", ephemeral=True)
                return

            append_runcli_audit(
                self.audit_log_path,
                {
                    "event": "rejected",
                    "guild_id": self.guild_id,
                    "channel_id": self.channel_id,
                    "requester_id": self.requester_id,
                    "actor_id": interaction.user.id,
                    "command": self.command_text,
                },
            )
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

    @tree.command(name="profile_show", description="保存されたユーザープロファイルを表示します")
    @app_commands.describe(limit="表示件数(1-50)")
    async def profile_show(
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 50] = 20,
    ) -> None:
        if interaction.guild_id is None or interaction.guild_id not in allowed_guild_ids:
            await interaction.response.send_message(
                "このサーバーではこのBotを利用できません。",
                ephemeral=True,
            )
            return
        if not persona_memory_enabled:
            await interaction.response.send_message("ペルソナ記憶は無効です。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            facts = await orchestrator.memory.get_user_profile_facts(user_id=interaction.user.id, limit=int(limit))
            if not facts:
                await interaction.followup.send("保存されたプロファイルはありません。", ephemeral=True)
                return

            lines = [f"user_id: {interaction.user.id}", f"facts: {len(facts)}", ""]
            for idx, fact in enumerate(facts, start=1):
                key = str(fact.get("key", ""))
                value = str(fact.get("value", ""))
                if len(value) > 140:
                    value = value[:137] + "..."
                updated = str(fact.get("updated_at", ""))
                lines.append(f"{idx}. {key} = {value} (updated={updated})")

            body = "\n".join(lines)
            for chunk in chunk_text(body, 1800):
                await interaction.followup.send(chunk, ephemeral=True)
        except Exception:
            logger.exception("Failed to handle /profile_show")
            await interaction.followup.send("プロフィール取得に失敗しました。", ephemeral=True)

    @tree.command(name="profile_set", description="ユーザープロファイルを保存または更新します")
    @app_commands.describe(key="項目名", value="値")
    async def profile_set(interaction: discord.Interaction, key: str, value: str) -> None:
        if interaction.guild_id is None or interaction.guild_id not in allowed_guild_ids:
            await interaction.response.send_message(
                "このサーバーではこのBotを利用できません。",
                ephemeral=True,
            )
            return
        if not persona_memory_enabled:
            await interaction.response.send_message("ペルソナ記憶は無効です。", ephemeral=True)
            return

        clean_key = (key or "").strip().lower()
        clean_value = (value or "").strip()
        if not clean_key or not clean_value:
            await interaction.response.send_message("key/value は空にできません。", ephemeral=True)
            return
        if len(clean_key) > 48:
            await interaction.response.send_message("key は48文字以内で指定してください。", ephemeral=True)
            return
        if len(clean_value) > 500:
            await interaction.response.send_message("value は500文字以内で指定してください。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await orchestrator.memory.set_user_profile_fact(
                user_id=interaction.user.id,
                key=clean_key,
                value=clean_value,
                source="manual",
                confirmed=True,
            )
            await interaction.followup.send(f"保存しました: {clean_key}", ephemeral=True)
        except Exception:
            logger.exception("Failed to handle /profile_set")
            await interaction.followup.send("プロフィール保存に失敗しました。", ephemeral=True)

    @tree.command(name="profile_forget", description="ユーザープロファイルを削除します")
    @app_commands.describe(key="削除する項目名（未指定で全削除）")
    async def profile_forget(interaction: discord.Interaction, key: str = "") -> None:
        if interaction.guild_id is None or interaction.guild_id not in allowed_guild_ids:
            await interaction.response.send_message(
                "このサーバーではこのBotを利用できません。",
                ephemeral=True,
            )
            return
        if not persona_memory_enabled:
            await interaction.response.send_message("ペルソナ記憶は無効です。", ephemeral=True)
            return

        clean_key = (key or "").strip().lower()
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            deleted = await orchestrator.memory.forget_user_profile_fact(
                user_id=interaction.user.id,
                key=clean_key if clean_key else None,
            )
            if clean_key:
                await interaction.followup.send(
                    f"項目削除: {clean_key} (deleted={deleted})",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"プロフィール全削除を実行しました (deleted={deleted})",
                    ephemeral=True,
                )
        except Exception:
            logger.exception("Failed to handle /profile_forget")
            await interaction.followup.send("プロフィール削除に失敗しました。", ephemeral=True)

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
        append_runcli_audit(
            runcli_audit_log_path,
            {
                "event": "requested",
                "guild_id": interaction.guild_id,
                "channel_id": interaction.channel_id,
                "requester_id": requester_id,
                "command": clean_command,
            },
        )
        view = CliApprovalView(
            command_text=clean_command,
            requester_id=requester_id,
            approver_ids=cli_approver_user_ids,
            guild_id=int(interaction.guild_id),
            channel_id=int(interaction.channel_id),
            audit_log_path=runcli_audit_log_path,
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

    @tree.command(name="runcli_audit", description="runcli監査ログの直近イベントを確認します")
    @app_commands.describe(limit="取得件数(1-50)", event="event種別フィルタ")
    @app_commands.choices(
        event=[
            app_commands.Choice(name="default", value="default"),
            app_commands.Choice(name="all", value="all"),
            app_commands.Choice(name="requested", value="requested"),
            app_commands.Choice(name="approved", value="approved"),
            app_commands.Choice(name="rejected", value="rejected"),
            app_commands.Choice(name="executed", value="executed"),
            app_commands.Choice(name="unauthorized_approve", value="unauthorized_approve"),
            app_commands.Choice(name="unauthorized_reject", value="unauthorized_reject"),
        ]
    )
    async def runcli_audit(
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 50] | None = None,
        event: app_commands.Choice[str] | None = None,
    ) -> None:
        if interaction.guild_id is None or interaction.guild_id not in allowed_guild_ids:
            await interaction.response.send_message(
                "このサーバーではこのBotを利用できません。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            fetch_limit = int(limit) if limit is not None else runcli_audit_tail_default
            rows = await asyncio.to_thread(read_runcli_audit_tail, runcli_audit_log_path, fetch_limit)
            selected_event = event.value if event is not None else "default"
            event_filter = runcli_audit_event_filter_default if selected_event == "default" else selected_event
            if event_filter != "all":
                rows = [row for row in rows if str(row.get("event", "")).strip().lower() == event_filter]
            if not rows:
                await interaction.followup.send("監査ログはまだありません。", ephemeral=True)
                return

            lines = [
                f"path: {runcli_audit_log_path}",
                f"event_filter: {event_filter}",
                f"events: {len(rows)}",
                "",
            ]
            for idx, row in enumerate(rows, start=1):
                ts = str(row.get("ts", "-"))
                event = str(row.get("event", "-"))
                actor = str(row.get("actor_id", row.get("requester_id", "-")))
                command = str(row.get("command", "-"))
                if len(command) > 64:
                    command = command[:61] + "..."
                lines.append(f"{idx}. [{ts}] {event} actor={actor} cmd={command}")

            body = "\n".join(lines)
            for chunk in chunk_text(body, 1800):
                await interaction.followup.send(chunk, ephemeral=True)
        except Exception:
            logger.exception("Failed to handle /runcli_audit")
            await interaction.followup.send("監査ログの取得に失敗しました。", ephemeral=True)

    @tree.command(name="readurl", description="URL本文を取得して確認します")
    @app_commands.describe(url="読み取り対象URL")
    async def readurl(interaction: discord.Interaction, url: str) -> None:
        if interaction.guild_id is None or interaction.guild_id not in allowed_guild_ids:
            await interaction.response.send_message(
                "このサーバーではこのBotを利用できません。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            result = await orchestrator.execute_tool_job(
                tool_name="read_url_markdown",
                args={"url": url},
                task_label="readurl",
            )
            await send_response(interaction, result, max_message_len=max_message_len)
        except Exception:
            logger.exception("Failed to handle /readurl")
            await interaction.followup.send("URL本文の取得に失敗しました。")

    @tree.command(name="deepdive", description="ソース特化で調査します")
    @app_commands.describe(topic="調査トピック", source="対象ソース")
    @app_commands.choices(
        source=[
            app_commands.Choice(name="auto", value="auto"),
            app_commands.Choice(name="github", value="github"),
            app_commands.Choice(name="reddit", value="reddit"),
            app_commands.Choice(name="youtube", value="youtube"),
            app_commands.Choice(name="x", value="x"),
        ]
    )
    async def deepdive(
        interaction: discord.Interaction,
        topic: str,
        source: app_commands.Choice[str] | None = None,
    ) -> None:
        if interaction.guild_id is None or interaction.guild_id not in allowed_guild_ids:
            await interaction.response.send_message(
                "このサーバーではこのBotを利用できません。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            source_value = source.value if source is not None else "auto"
            if deepdive_use_research_agent:
                result = await orchestrator.execute_tool_job(
                    tool_name="dispatch_research_job",
                    args={"topic": topic, "source": source_value, "wait": "false"},
                    task_label="deepdive:research",
                )
                try:
                    payload = json.loads(result)
                except Exception:
                    payload = {}

                if (
                    research_notify_on_complete
                    and isinstance(payload, dict)
                    and str(payload.get("status", "")).strip().lower() == "queued"
                ):
                    job_id = str(payload.get("job_id", "")).strip()
                    if job_id:
                        append_research_audit(
                            research_audit_log_path,
                            {
                                "event": "submitted",
                                "job_id": job_id,
                                "topic": topic,
                                "source": source_value,
                                "channel_id": int(interaction.channel_id),
                                "actor_id": interaction.user.id if interaction.user else 0,
                            },
                        )
                        await _start_research_notification(
                            job_id=job_id,
                            topic=topic,
                            source=source_value,
                            channel_id=int(interaction.channel_id),
                        )
                        result = (
                            "Research Agent にジョブを投入しました。\n"
                            f"job_id: {job_id}\n"
                            "完了したらこのチャンネルへ自動通知します。"
                        )
            else:
                result = await orchestrator.execute_tool_job(
                    tool_name="source_deep_dive",
                    args={"topic": topic, "source": source_value},
                    task_label="deepdive",
                )
            await send_response(interaction, result, max_message_len=max_message_len)
        except Exception:
            logger.exception("Failed to handle /deepdive")
            await interaction.followup.send("deep diveの実行に失敗しました。")

    @tree.command(name="logsearch", description="Discord過去ログをキーワード検索します")
    @app_commands.describe(keyword="検索キーワード", scope="検索範囲", limit="表示件数(1-12)")
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="default", value="default"),
            app_commands.Choice(name="channel", value="channel"),
            app_commands.Choice(name="guild", value="guild"),
        ]
    )
    async def logsearch(
        interaction: discord.Interaction,
        keyword: str,
        scope: app_commands.Choice[str] | None = None,
        limit: app_commands.Range[int, 1, 12] = 6,
    ) -> None:
        if interaction.guild_id is None or interaction.guild_id not in allowed_guild_ids:
            await interaction.response.send_message(
                "このサーバーではこのBotを利用できません。",
                ephemeral=True,
            )
            return

        clean_keyword = (keyword or "").strip()
        if not clean_keyword:
            await interaction.response.send_message("キーワードが空です。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            selected_scope = scope.value if scope is not None else "default"
            query_scope = logsearch_default_scope if selected_scope == "default" else selected_scope

            records = await orchestrator.memory.fetch_relevant_messages(
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                query_text=clean_keyword,
                limit=int(limit),
                scope=query_scope,
            )

            if not records:
                await interaction.followup.send("該当する過去ログは見つかりませんでした。", ephemeral=True)
                return

            lines = [f"keyword: {clean_keyword}", f"scope: {query_scope}", f"hits: {len(records)}", ""]
            for idx, record in enumerate(records, start=1):
                md = record.metadata or {}
                ch_name = str(md.get("channel_name", "")).strip()
                ch_id = str(md.get("channel_id", "")).strip()
                ch_label = f"#{ch_name}" if ch_name else f"ch={ch_id or 'unknown'}"
                snippet = (record.content or "").replace("\n", " ").strip()
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."
                if logsearch_include_score:
                    overlap = _logsearch_overlap_score(clean_keyword, record.content or "")
                    recency = _logsearch_recency_score(record.timestamp)
                    final_score = (score_overlap_weight * overlap) + (score_recency_weight * recency)
                    lines.append(
                        f"{idx}. [{record.timestamp}] {ch_label} {record.role}: {snippet} "
                        f"(score={final_score:.2f}, overlap={overlap:.2f}, recency={recency:.2f})"
                    )
                else:
                    lines.append(f"{idx}. [{record.timestamp}] {ch_label} {record.role}: {snippet}")

            body = "\n".join(lines)
            for chunk in chunk_text(body, 1800):
                await interaction.followup.send(chunk, ephemeral=True)
        except Exception:
            logger.exception("Failed to handle /logsearch")
            await interaction.followup.send("ログ検索に失敗しました。", ephemeral=True)

    debug_operator_user_ids = {
        int(part.strip())
        for part in os.getenv("DEBUG_OPERATOR_USER_IDS", os.getenv("CLI_APPROVER_USER_IDS", "")).split(",")
        if part.strip().isdigit()
    }
    command_allowlist = {
        part.strip()
        for part in os.getenv("DISCORD_COMMAND_ALLOWLIST", "ask,auth_status,debug_action").split(",")
        if part.strip()
    }

    @tree.command(name="auth_status", description="外部連携の認証設定状況を表示します")
    async def auth_status(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or interaction.guild_id not in allowed_guild_ids:
            await interaction.response.send_message(
                "このサーバーではこのBotを利用できません。",
                ephemeral=True,
            )
            return

        enabled_actions = {
            part.strip()
            for part in os.getenv("INTERNAL_ALLOWED_ACTIONS", "").split(",")
            if part.strip()
        }
        calendar_provider = (os.getenv("CALENDAR_PROVIDER", "google").strip().lower() or "google")
        calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary").strip() or "primary"
        calendar_creds_ready = all(
            bool(os.getenv(key, "").strip())
            for key in [
                "GOOGLE_CALENDAR_CLIENT_ID",
                "GOOGLE_CALENDAR_CLIENT_SECRET",
                "GOOGLE_CALENDAR_REFRESH_TOKEN",
            ]
        )
        calendar_auth_url = os.getenv(
            "GOOGLE_CALENDAR_AUTH_URL",
            "https://console.cloud.google.com/apis/credentials",
        ).strip()

        github_token_set = bool(os.getenv("GITHUB_TOKEN", "").strip())
        smtp_ready = all(
            bool(os.getenv(key, "").strip())
            for key in ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"]
        )
        smtp_enabled = "send_email" in enabled_actions
        github_auth_url = os.getenv("GITHUB_AUTH_URL", "https://github.com/settings/tokens").strip()
        smtp_auth_url = os.getenv("SMTP_AUTH_URL", "").strip() or "(未設定)"

        smtp_line = (
            f"- SMTP credentials (optional): {'configured' if smtp_ready else 'missing'}\n"
            f"- SMTP auth URL: {smtp_auth_url}\n"
            f"- SMTP action enabled: {'yes' if smtp_enabled else 'no'}\n"
        )
        calendar_line = (
            f"- Calendar provider: {calendar_provider}\n"
            f"- Calendar ID: {calendar_id}\n"
            f"- Google Calendar credentials: {'configured' if calendar_creds_ready else 'missing'}\n"
            f"- Google Calendar auth URL: {calendar_auth_url}\n"
        )

        body = (
            "認証設定ステータス\n"
            f"- GitHub token: {'configured' if github_token_set else 'missing'}\n"
            f"- GitHub auth URL: {github_auth_url}\n"
            f"{calendar_line}"
            f"{smtp_line}"
            "\n"
            "通常運用は /ask を使ってください。"
        )
        await interaction.response.send_message(body, ephemeral=True)

    @tree.command(name="debug_action", description="デバッグ用: actionを手動実行します")
    @app_commands.describe(action="action名", payload_json="JSONオブジェクト文字列")
    async def debug_action_command(
        interaction: discord.Interaction,
        action: str,
        payload_json: str = "{}",
    ) -> None:
        if interaction.guild_id is None or interaction.guild_id not in allowed_guild_ids:
            await interaction.response.send_message(
                "このサーバーではこのBotを利用できません。",
                ephemeral=True,
            )
            return

        if debug_operator_user_ids and interaction.user.id not in debug_operator_user_ids:
            await interaction.response.send_message(
                "このコマンドはデバッグ担当者のみ利用できます。通常は /ask を使ってください。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await orchestrator.execute_tool_job(
                tool_name="execute_internal_action",
                args={"action": action, "payload_json": payload_json},
                task_label="debug_action",
            )
            for chunk in chunk_text(result, 1800):
                await interaction.followup.send(chunk, ephemeral=True)
        except Exception:
            logger.exception("Failed to handle /debug_action")
            await interaction.followup.send("action の実行に失敗しました。", ephemeral=True)

    @client.event
    async def on_ready() -> None:
        logger.info("Logged in as %s (%s)", client.user, client.user.id if client.user else "unknown")

        removed_global_defs: list[str] = []
        for command in list(tree.get_commands(guild=None)):
            if command.name in command_allowlist:
                continue
            tree.remove_command(command.name)
            removed_global_defs.append(command.name)
        if removed_global_defs:
            logger.info("Pruned local commands by allowlist: %s", sorted(set(removed_global_defs)))

        try:
            app_id = client.application_id
            if app_id is not None:
                await tree._http.bulk_upsert_global_commands(app_id, payload=[])
                logger.info("Global commands purged before guild sync")
            else:
                logger.warning("Skip global purge: application_id is None")
        except Exception:
            logger.exception("Failed to purge global commands")

        synced: list[int] = []
        failed: list[int] = []
        connected_guild_ids = {g.id for g in client.guilds}
        sync_targets = sorted(gid for gid in allowed_guild_ids if gid in connected_guild_ids)
        skipped_targets = sorted(gid for gid in allowed_guild_ids if gid not in connected_guild_ids)

        for guild_id in sync_targets:
            try:
                guild = discord.Object(id=guild_id)
                tree.clear_commands(guild=guild)
                tree.copy_global_to(guild=guild)
                for command in list(tree.get_commands(guild=guild)):
                    if command.name in command_allowlist:
                        continue
                    tree.remove_command(command.name, guild=guild)
                await tree.sync(guild=guild)
                synced.append(guild_id)
            except Exception:
                failed.append(guild_id)
                logger.exception("Failed to sync commands for guild: %s", guild_id)

        if synced:
            logger.info("Command sync completed for guilds: %s", synced)
        if failed:
            logger.warning("Command sync failed for guilds: %s", failed)
        if skipped_targets:
            logger.warning("Command sync skipped (bot has no access) for guilds: %s", skipped_targets)

        if research_notify_on_complete:
            try:
                checkpoints = await orchestrator.list_workflow_checkpoints(
                    workflow="research_job",
                    status="queued",
                    limit=20,
                )
                resumed = 0
                for cp in checkpoints:
                    payload = cp.get("payload", {}) if isinstance(cp, dict) else {}
                    if not isinstance(payload, dict):
                        continue
                    job_id = str(cp.get("job_id", "")).strip() or str(payload.get("job_id", "")).strip()
                    topic = str(payload.get("topic", "")).strip()
                    source = str(payload.get("source", "auto")).strip() or "auto"
                    channel_id = int(payload.get("channel_id", 0) or 0)
                    if not job_id or not topic or channel_id <= 0:
                        continue
                    await _start_research_notification(
                        job_id=job_id,
                        topic=topic,
                        source=source,
                        channel_id=channel_id,
                    )
                    resumed += 1
                if resumed > 0:
                    logger.info("Resumed research notify tasks from checkpoints: %s", resumed)
            except Exception:
                logger.exception("Failed to resume research notify tasks")

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

        bot_id = int(client.user.id)
        mention_prefix_pattern = re.compile(rf"^\s*<@!?{bot_id}>\s*")
        mention_any_pattern = re.compile(rf"<@!?{bot_id}>")
        if mention_require_prefix and mention_prefix_pattern.search(content) is None:
            return

        question = mention_any_pattern.sub("", message.content or "").strip()
        if not question:
            try:
                await message.reply("メンションの後ろに質問内容を書いてください。", mention_author=False)
            except Exception:
                logger.exception("Failed to send mention usage hint")
            return

        if mention_quick_calendar_enabled:
            quick_action = build_quick_calendar_action(question)
            if quick_action is not None:
                action_name, payload = quick_action
                try:
                    result = await orchestrator.execute_tool_job(
                        tool_name="execute_internal_action",
                        args={
                            "action": action_name,
                            "payload_json": json.dumps(payload, ensure_ascii=False),
                        },
                        task_label=f"mention_quick:{action_name}",
                    )
                    await send_message_response(message, result, max_message_len=max_message_len)
                    return
                except Exception:
                    logger.exception("Failed to handle mention quick calendar action")

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
