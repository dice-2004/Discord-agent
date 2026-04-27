from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from main_agent.core.memory import ChannelMemoryStore, MemoryRecord

try:
    from google import genai as google_genai
    from google.genai import types as google_genai_types

    _HAS_GOOGLE_GENAI = True
except ImportError:
    _HAS_GOOGLE_GENAI = False

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class UserProfileAnalyzerConfig:
    enabled: bool = False
    target_user_id: int = 0
    target_guild_id: int = 0
    context_channel_id: int = 0
    analyze_interval_sec: int = 1800
    sample_limit: int = 200
    summary_path: str = "./data/runtime/user_profile_summary.json"
    gemini_api_key: str = ""
    gemini_model: str = "gemma-4-31b-it"
    proxy_request_enabled: bool = False


def _safe_int_env(name: str, default_value: int) -> int:
    raw = os.getenv(name, str(default_value)).strip()
    try:
        return int(raw)
    except ValueError:
        return default_value


def load_user_profile_analyzer_config_from_env() -> UserProfileAnalyzerConfig:
    fallback_key = (
        os.getenv("PROFILE_ANALYZER_GEMINI_API_KEY", "").strip()
        or os.getenv("TWITTER_SCOUT_GEMINI_API_KEY", "").strip()
        or os.getenv("MAIN_AGENT_GEMINI_API_KEY", "").strip()
        or os.getenv("GEMINI_API_KEY", "").strip()
    )
    return UserProfileAnalyzerConfig(
        enabled=os.getenv("PROFILE_ANALYZER_ENABLED", "false").strip().lower() == "true",
        target_user_id=max(0, _safe_int_env("PROFILE_ANALYZER_USER_ID", 0)),
        target_guild_id=max(0, _safe_int_env("PROFILE_ANALYZER_GUILD_ID", 0)),
        context_channel_id=max(0, _safe_int_env("PROFILE_ANALYZER_CHANNEL_ID", 0)),
        analyze_interval_sec=max(300, _safe_int_env("PROFILE_ANALYZER_INTERVAL_SEC", 1800)),
        sample_limit=max(20, min(400, _safe_int_env("PROFILE_ANALYZER_SAMPLE_LIMIT", 200))),
        summary_path=os.getenv("PROFILE_ANALYZER_SUMMARY_PATH", "./data/runtime/user_profile_summary.json").strip()
        or "./data/runtime/user_profile_summary.json",
        gemini_api_key=fallback_key,
        gemini_model=os.getenv("PROFILE_ANALYZER_GEMINI_MODEL", "gemma-4-31b-it").strip() or "gemma-4-31b-it",
        proxy_request_enabled=os.getenv("PROFILE_AGENT_PROXY_REQUEST_ENABLED", "false").strip().lower() == "true",
    )


class UserProfileAnalyzerAgent:
    """Analyzes a single user's Discord usage patterns and updates persona facts."""

    def __init__(self, config: UserProfileAnalyzerConfig, memory_store: ChannelMemoryStore) -> None:
        self.config = config
        self.memory_store = memory_store
        self._summary_path = Path(config.summary_path)
        self._last_summary: dict[str, Any] = {}
        self._model_client: Any | None = None
        if _HAS_GOOGLE_GENAI and self.config.gemini_api_key:
            try:
                self._model_client = google_genai.Client(api_key=self.config.gemini_api_key)
            except Exception:
                logger.exception("Failed to initialize profile analyzer model client")

    async def analyze_once(self) -> dict[str, Any]:
        if not self.config.enabled:
            return {"status": "disabled"}
        if self.config.target_user_id <= 0:
            return {"status": "missing_user_id"}

        guild_id = self.config.target_guild_id if self.config.target_guild_id > 0 else None
        channel_id = self.config.context_channel_id if self.config.context_channel_id > 0 else 0

        messages = await self.memory_store.get_user_messages(
            user_id=self.config.target_user_id,
            guild_id=guild_id,
            channel_id=channel_id,
            limit=self.config.sample_limit,
            scope="guild",
        )
        if not messages:
            summary = {
                "status": "no_messages",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "user_id": self.config.target_user_id,
            }
            await self._save_summary(summary)
            return summary

        heuristics = self._analyze_heuristics(messages)
        llm_summary = await asyncio.to_thread(self._build_llm_summary, heuristics, messages)
        merged = self._merge_summary(heuristics, llm_summary)

        await self._persist_profile_facts(merged)
        await self._save_summary(merged)
        return merged

    async def get_last_summary(self) -> dict[str, Any]:
        if self._last_summary:
            return json.loads(json.dumps(self._last_summary, ensure_ascii=False))

        if not self._summary_path.exists():
            return {}
        try:
            text = await asyncio.to_thread(self._summary_path.read_text, "utf-8")
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                self._last_summary = loaded
                return loaded
        except Exception:
            logger.exception("Failed to read user profile summary from disk")
        return {}

    def _analyze_heuristics(self, messages: list[MemoryRecord]) -> dict[str, Any]:
        hours = Counter()
        token_counter = Counter()
        category_counter = Counter()

        category_rules = {
            "research": ("調査", "調べ", "deepdive", "深掘", "analysis", "比較"),
            "automation": ("自動", "loop", "cron", "定期", "通知", "監視"),
            "dev": ("実装", "コード", "bug", "debug", "エラー", "python", "docker"),
            "ops": ("deploy", "運用", "ログ", "監査", "ステータス", "復旧"),
        }

        for message in messages:
            text = (message.content or "").strip()
            if not text:
                continue

            ts = str(message.timestamp or "")
            try:
                parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                hours[int(parsed.astimezone(timezone.utc).hour)] += 1
            except Exception:
                pass

            for token in self._tokenize(text):
                token_counter[token] += 1

            lower = text.lower()
            for category, keywords in category_rules.items():
                if any(k in lower for k in keywords):
                    category_counter[category] += 1

        active_hours = [hour for hour, _ in hours.most_common(4)]
        top_tokens = [token for token, count in token_counter.most_common(20) if count >= 2][:10]
        top_categories = [category for category, _ in category_counter.most_common(5)]

        return {
            "status": "ok",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "user_id": self.config.target_user_id,
            "sample_message_count": len(messages),
            "active_hours_utc": active_hours,
            "top_tokens": top_tokens,
            "top_categories": top_categories,
            "recent_samples": [m.content[:180] for m in messages[:25]],
        }

    def _tokenize(self, text: str) -> list[str]:
        tokens = re.findall(r"[a-zA-Z0-9_\-]{3,}|[一-龥]{2,}|[ぁ-ん]{2,}|[ァ-ンー]{2,}", text.lower())
        stop_words = {
            "です",
            "ます",
            "する",
            "して",
            "ある",
            "ない",
            "これ",
            "それ",
            "ため",
            "こと",
            "with",
            "from",
            "that",
            "this",
            "then",
        }
        return [token for token in tokens if token not in stop_words and len(token) >= 2]

    def _build_llm_summary(self, heuristics: dict[str, Any], messages: list[MemoryRecord]) -> dict[str, Any]:
        if self._model_client is None:
            return {}

        prompt = (
            "次の Discord 利用傾向から、ユーザのパーソナライズ用プロファイルを JSON で作成してください。\n"
            "出力はJSONのみ。\n"
            "形式: {\"request_style\":string,\"time_habit\":string,\"high_value_topics\":string[],\"personalization_guidelines\":string[],\"proxy_request_candidates\":string[]}\n"
            "proxy_request_candidates は必要時に代理実行すべき依頼の候補を3件以内で。\n\n"
            f"heuristics={json.dumps(heuristics, ensure_ascii=False)}\n"
            f"recent_samples={json.dumps([m.content[:300] for m in messages[:30]], ensure_ascii=False)}"
        )

        try:
            response = self._model_client.models.generate_content(
                model=self.config.gemini_model,
                contents=prompt,
                config=google_genai_types.GenerateContentConfig(
                    temperature=0.2,
                    top_p=0.95,
                    top_k=40,
                    max_output_tokens=2048,
                ),
            )
            text = (getattr(response, "text", "") or "").strip()
            parsed = self._extract_json(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            logger.exception("Profile analyzer model call failed")
        return {}

    def _extract_json(self, text: str) -> dict[str, Any] | None:
        clean = (text or "").strip()
        if not clean:
            return None
        try:
            parsed = json.loads(clean)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        match = re.search(r"\{.*\}", clean, flags=re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None
        return None

    def _merge_summary(self, heuristics: dict[str, Any], llm_summary: dict[str, Any]) -> dict[str, Any]:
        merged = dict(heuristics)
        merged["request_style"] = str(llm_summary.get("request_style", "")).strip()[:600]
        merged["time_habit"] = str(llm_summary.get("time_habit", "")).strip()[:600]
        merged["high_value_topics"] = (
            [str(x).strip() for x in llm_summary.get("high_value_topics", []) if str(x).strip()][:12]
            if isinstance(llm_summary.get("high_value_topics", []), list)
            else []
        )
        merged["personalization_guidelines"] = (
            [str(x).strip() for x in llm_summary.get("personalization_guidelines", []) if str(x).strip()][:12]
            if isinstance(llm_summary.get("personalization_guidelines", []), list)
            else []
        )
        merged["proxy_request_candidates"] = (
            [str(x).strip() for x in llm_summary.get("proxy_request_candidates", []) if str(x).strip()][:3]
            if isinstance(llm_summary.get("proxy_request_candidates", []), list)
            else []
        )
        return merged

    async def _persist_profile_facts(self, summary: dict[str, Any]) -> None:
        uid = self.config.target_user_id
        await self.memory_store.set_user_profile_fact(
            user_id=uid,
            key="request_style",
            value=str(summary.get("request_style", "") or "(unknown)"),
            source="profile_analyzer_agent",
            confirmed=True,
        )
        await self.memory_store.set_user_profile_fact(
            user_id=uid,
            key="time_habit",
            value=str(summary.get("time_habit", "") or "(unknown)"),
            source="profile_analyzer_agent",
            confirmed=True,
        )
        await self.memory_store.set_user_profile_fact(
            user_id=uid,
            key="frequent_topics",
            value=", ".join(summary.get("high_value_topics", []) or summary.get("top_tokens", [])[:6])[:800],
            source="profile_analyzer_agent",
            confirmed=True,
        )
        await self.memory_store.set_user_profile_fact(
            user_id=uid,
            key="personalization_guidelines",
            value=" | ".join(summary.get("personalization_guidelines", [])[:5])[:1200],
            source="profile_analyzer_agent",
            confirmed=True,
        )

    async def _save_summary(self, payload: dict[str, Any]) -> None:
        self._last_summary = payload
        try:
            self._summary_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                self._summary_path.write_text,
                json.dumps(payload, ensure_ascii=False, indent=2),
                "utf-8",
            )
        except Exception:
            logger.exception("Failed to persist user profile summary: %s", self._summary_path)
