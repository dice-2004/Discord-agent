from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.deep_dive_tools import source_deep_dive

try:
    from google import genai as google_genai
    from google.genai import types as google_genai_types

    _HAS_GOOGLE_GENAI = True
except ImportError:
    _HAS_GOOGLE_GENAI = False

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TwitterScoutConfig:
    enabled: bool = False
    gemini_api_key: str = ""
    gemini_model: str = "gemma-4-31b-it"
    crawl_interval_sec: int = 180
    topics: list[str] | None = None
    max_topics_per_cycle: int = 2
    max_requests_per_day: int = 1500
    snapshot_path: str = "./data/runtime/twitter_scout_snapshot.json"
    profile_path: str = "./data/profiles/initial_profile.md"


def _parse_csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    seen: set[str] = set()
    items: list[str] = []
    for part in raw.split(","):
        value = part.strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(value)
    return items


def _safe_int_env(name: str, default_value: int) -> int:
    raw = os.getenv(name, str(default_value)).strip()
    try:
        return int(raw)
    except ValueError:
        return default_value


def load_twitter_scout_config_from_env() -> TwitterScoutConfig:
    topics = _parse_csv_env("TWITTER_SCOUT_TOPICS")
    return TwitterScoutConfig(
        enabled=os.getenv("TWITTER_SCOUT_ENABLED", "false").strip().lower() == "true",
        gemini_api_key=os.getenv("TWITTER_SCOUT_GEMINI_API_KEY", "").strip(),
        gemini_model=os.getenv("TWITTER_SCOUT_GEMINI_MODEL", "gemma-4-31b-it").strip() or "gemma-4-31b-it",
        crawl_interval_sec=max(30, _safe_int_env("TWITTER_SCOUT_CRAWL_INTERVAL_SEC", 180)),
        topics=topics,
        max_topics_per_cycle=max(1, _safe_int_env("TWITTER_SCOUT_MAX_TOPICS_PER_CYCLE", 2)),
        max_requests_per_day=max(1, _safe_int_env("TWITTER_SCOUT_MAX_REQUESTS_PER_DAY", 1500)),
        snapshot_path=os.getenv("TWITTER_SCOUT_SNAPSHOT_PATH", "./data/runtime/twitter_scout_snapshot.json").strip()
        or "./data/runtime/twitter_scout_snapshot.json",
        profile_path=os.getenv("INITIAL_PROFILE_PATH", "./data/profiles/initial_profile.md").strip()
        or "./data/profiles/initial_profile.md",
    )


class TwitterScoutAgent:
    """Continuously scouts X(Twitter)-related topics and keeps recommendation snapshots."""

    def __init__(self, config: TwitterScoutConfig) -> None:
        self.config = config
        self._snapshot_path = Path(config.snapshot_path)
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._topic_cursor = 0
        self._daily_usage_date = ""
        self._daily_usage_count = 0
        self._snapshot: dict[str, Any] = {
            "updated_at": "",
            "status": "idle",
            "selected_topics": [],
            "recommendations": [],
            "summary": "",
            "budget": {
                "date": "",
                "used": 0,
                "max": self.config.max_requests_per_day,
            },
        }
        self._model_client: Any | None = None
        if _HAS_GOOGLE_GENAI and self.config.gemini_api_key:
            try:
                self._model_client = google_genai.Client(api_key=self.config.gemini_api_key)
            except Exception:
                logger.exception("Failed to initialize TwitterScout model client")

    async def start(self) -> None:
        if not self.config.enabled:
            return
        if self._task is not None and not self._task.done():
            return

        await self._load_snapshot_from_disk()
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="twitter-scout-agent")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is not None:
            try:
                await task
            except Exception:
                logger.exception("TwitterScout task ended with error")

    async def refresh_now(self) -> dict[str, Any]:
        await self._crawl_once()
        return await self.get_snapshot()

    async def get_snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return json.loads(json.dumps(self._snapshot, ensure_ascii=False))

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._crawl_once()
            except Exception:
                logger.exception("TwitterScout crawl failed")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.config.crawl_interval_sec)
            except asyncio.TimeoutError:
                pass

    async def _crawl_once(self) -> None:
        selected_topics = self._select_topics_for_cycle()
        if not selected_topics:
            await self._update_snapshot(
                {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "status": "no_topics",
                    "selected_topics": [],
                    "recommendations": [],
                    "summary": "TWITTER_SCOUT_TOPICS または profile から巡回テーマを解決できませんでした。",
                    "budget": self._budget_payload(),
                }
            )
            return

        raw_blocks: list[dict[str, str]] = []
        for topic in selected_topics:
            raw = await asyncio.to_thread(source_deep_dive, topic, "x")
            raw_blocks.append(
                {
                    "topic": topic,
                    "result": str(raw or ""),
                }
            )

        summarized = await asyncio.to_thread(self._summarize_results, selected_topics, raw_blocks)
        updated_at = datetime.now(timezone.utc).isoformat()

        payload = {
            "updated_at": updated_at,
            "status": "ok",
            "selected_topics": selected_topics,
            "recommendations": summarized.get("recommendations", []),
            "summary": summarized.get("summary", ""),
            "profile_keywords": summarized.get("profile_keywords", []),
            "raw_excerpt": self._build_raw_excerpt(raw_blocks),
            "budget": self._budget_payload(),
        }
        await self._update_snapshot(payload)

    async def _update_snapshot(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            self._snapshot = payload
            await asyncio.to_thread(self._persist_snapshot_sync, payload)

    def _persist_snapshot_sync(self, payload: dict[str, Any]) -> None:
        try:
            self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            self._snapshot_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("Failed to persist twitter scout snapshot: %s", self._snapshot_path)

    async def _load_snapshot_from_disk(self) -> None:
        if not self._snapshot_path.exists():
            return
        try:
            text = await asyncio.to_thread(self._snapshot_path.read_text, "utf-8")
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                async with self._lock:
                    self._snapshot = loaded
        except Exception:
            logger.exception("Failed to load twitter scout snapshot: %s", self._snapshot_path)

    def _select_topics_for_cycle(self) -> list[str]:
        topics = list(self.config.topics or [])
        if not topics:
            topics = self._extract_topics_from_profile(self.config.profile_path)

        if not topics:
            return []

        count = min(len(topics), max(1, self.config.max_topics_per_cycle))
        selected: list[str] = []
        for _ in range(count):
            idx = self._topic_cursor % len(topics)
            selected.append(topics[idx])
            self._topic_cursor += 1
        return selected

    def _extract_topics_from_profile(self, profile_path: str) -> list[str]:
        path = Path(profile_path)
        if not path.exists():
            return []

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return []

        candidates: list[str] = []
        for line in text.splitlines():
            cleaned = line.strip().lstrip("-*").strip()
            if not cleaned:
                continue
            if cleaned.startswith("#"):
                cleaned = cleaned.lstrip("#").strip()
            if len(cleaned) < 3 or len(cleaned) > 80:
                continue
            if re.search(r"api|token|password|secret", cleaned.lower()):
                continue
            candidates.append(cleaned)

        if not candidates:
            return []

        seen: set[str] = set()
        topics: list[str] = []
        for candidate in candidates:
            key = candidate.lower()
            if key in seen:
                continue
            seen.add(key)
            topics.append(candidate)
            if len(topics) >= 12:
                break
        return topics

    def _summarize_results(self, selected_topics: list[str], raw_blocks: list[dict[str, str]]) -> dict[str, Any]:
        prompt = self._build_summary_prompt(selected_topics, raw_blocks)
        model_output = self._call_model(prompt)
        if model_output is not None:
            return model_output
        return self._build_fallback_summary(selected_topics, raw_blocks)

    def _build_summary_prompt(self, selected_topics: list[str], raw_blocks: list[dict[str, str]]) -> str:
        blocks: list[str] = []
        for idx, block in enumerate(raw_blocks, start=1):
            blocks.append(f"[Topic {idx}] {block.get('topic', '')}\n{block.get('result', '')[:3000]}")

        return (
            "あなたは Twitter(X) 巡回エージェントです。\n"
            "次の巡回結果から、ユーザに通知する価値が高い話題を抽出してください。\n"
            "出力はJSONのみ。\n"
            "JSON形式: {\"summary\":string,\"profile_keywords\":string[],\"recommendations\":[{\"title\":string,\"why\":string,\"source_urls\":string[]}]}\n"
            "recommendations は最大5件。source_urls は x.com / twitter.com URLを優先。\n\n"
            f"selected_topics={json.dumps(selected_topics, ensure_ascii=False)}\n\n"
            + "\n\n".join(blocks)
        )

    def _call_model(self, prompt: str) -> dict[str, Any] | None:
        if self._model_client is None:
            return None
        if not self._consume_budget_token():
            return None

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
            parsed = self._extract_json_object(text)
            if not isinstance(parsed, dict):
                return None
            recs = parsed.get("recommendations", [])
            if not isinstance(recs, list):
                recs = []
            normalized_recs: list[dict[str, Any]] = []
            for rec in recs[:5]:
                if not isinstance(rec, dict):
                    continue
                urls = rec.get("source_urls", [])
                if not isinstance(urls, list):
                    urls = []
                normalized_recs.append(
                    {
                        "title": str(rec.get("title", "")).strip()[:160],
                        "why": str(rec.get("why", "")).strip()[:360],
                        "source_urls": [str(u).strip() for u in urls if str(u).strip()][:5],
                    }
                )
            return {
                "summary": str(parsed.get("summary", "")).strip()[:1200],
                "profile_keywords": [str(x).strip() for x in parsed.get("profile_keywords", []) if str(x).strip()][:12]
                if isinstance(parsed.get("profile_keywords", []), list)
                else [],
                "recommendations": normalized_recs,
            }
        except Exception:
            logger.exception("TwitterScout model call failed")
            return None

    def _build_fallback_summary(self, selected_topics: list[str], raw_blocks: list[dict[str, str]]) -> dict[str, Any]:
        recommendations: list[dict[str, Any]] = []
        for block in raw_blocks:
            urls = re.findall(r"https?://[^\s\)\]]+", block.get("result", ""))
            x_urls = [u for u in urls if "x.com" in u or "twitter.com" in u]
            recommendations.append(
                {
                    "title": f"{block.get('topic', '')} の最新反応",
                    "why": "モデル要約なしのため、巡回結果から URL を抽出しました。",
                    "source_urls": x_urls[:3],
                }
            )
            if len(recommendations) >= 5:
                break

        return {
            "summary": "モデル要約を使わず、巡回結果を簡易整形しました。",
            "profile_keywords": selected_topics[:8],
            "recommendations": recommendations,
        }

    def _build_raw_excerpt(self, raw_blocks: list[dict[str, str]]) -> str:
        lines: list[str] = []
        for block in raw_blocks:
            topic = block.get("topic", "")
            result = block.get("result", "")
            lines.append(f"[{topic}]\n{result[:1200]}")
        merged = "\n\n".join(lines)
        return merged[:6000]

    def _extract_json_object(self, text: str) -> dict[str, Any] | None:
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

    def _consume_budget_token(self) -> bool:
        current_date = datetime.now(timezone.utc).date().isoformat()
        if self._daily_usage_date != current_date:
            self._daily_usage_date = current_date
            self._daily_usage_count = 0

        if self._daily_usage_count >= self.config.max_requests_per_day:
            return False

        self._daily_usage_count += 1
        return True

    def _budget_payload(self) -> dict[str, Any]:
        current_date = datetime.now(timezone.utc).date().isoformat()
        if self._daily_usage_date != current_date:
            self._daily_usage_date = current_date
            self._daily_usage_count = 0

        return {
            "date": self._daily_usage_date,
            "used": self._daily_usage_count,
            "max": self.config.max_requests_per_day,
        }
