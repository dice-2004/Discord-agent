from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from tools import ToolRegistry, build_default_tool_registry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ResearchLoopResult:
    report: str
    transcript: str
    decision_log: list[dict[str, Any]]


def run_model_research_loop(
    *,
    topic: str,
    source: str,
    timeout_sec: int,
    model_name: str,
    model_call: Callable[[str], str],
    loop_label: str,
    tool_registry: ToolRegistry | None = None,
    max_turns: int | None = None,
) -> ResearchLoopResult:
    registry = tool_registry or build_default_tool_registry()
    target_turns = max_turns if max_turns is not None else max(4, min(18, max(2, timeout_sec // 60 + 3)))

    start_time = time.time()
    scratchpad: list[str] = []
    decision_log: list[dict[str, Any]] = []
    transcript_lines: list[str] = [
        f"[loop] {loop_label}",
        f"[model] {model_name}",
        f"[topic] {topic}",
        f"[source] {source}",
        f"[timeout_sec] {timeout_sec}",
        f"[max_turns] {target_turns}",
    ]
    tool_history: list[str] = []
    final_candidate = ""

    for turn in range(1, target_turns + 1):
        elapsed = time.time() - start_time
        prompt = _build_thinking_prompt(
            topic=topic,
            source=source,
            loop_label=loop_label,
            turn=turn,
            target_turns=target_turns,
            registry=registry,
            scratchpad=scratchpad,
        )
        transcript_lines.append(f"[turn {turn}] prompt_chars={len(prompt)} elapsed={elapsed:.1f}")
        response_text = model_call(prompt)
        transcript_lines.append(f"[turn {turn}] raw_response={response_text[:4000]}")

        decision = _extract_json_object(response_text) or {"action": "respond", "response": response_text}
        action = str(decision.get("action", "respond")).strip().lower()
        tool_name = str(decision.get("tool", "")).strip()
        reason = str(decision.get("reason", "")).strip()
        decision_log.append(
            {
                "turn": turn,
                "action": action,
                "tool": tool_name,
                "reason": reason,
                "elapsed_sec": round(elapsed, 2),
            }
        )
        transcript_lines.append(f"[turn {turn}] decision={json.dumps(decision, ensure_ascii=False)}")

        if action == "tool":
            tool_name, tool_args = _normalize_tool_call(topic=topic, source=source, tool_name=tool_name, decision=decision, turn=turn, scratchpad=scratchpad)
            tool_result = registry.execute(tool_name, tool_args)
            tool_summary = _summarize_tool_result(tool_result, tool_name)
            scratchpad.append(tool_summary)
            tool_history.append(tool_name)
            if len(tool_history) > 8:
                tool_history = tool_history[-8:]
            transcript_lines.append(f"[turn {turn}] tool={tool_name} args={json.dumps(tool_args, ensure_ascii=False)}")
            transcript_lines.append(f"[turn {turn}] tool_result={tool_summary[:4000]}")
            continue

        final_candidate = str(decision.get("response", "") or response_text).strip()
        if turn < target_turns and elapsed < timeout_sec:
            forced_tool, forced_args = _select_forced_tool(topic=topic, turn=turn, scratchpad=scratchpad)
            forced_result = registry.execute(forced_tool, forced_args)
            forced_summary = _summarize_tool_result(forced_result, forced_tool)
            scratchpad.append(forced_summary)
            transcript_lines.append(f"[turn {turn}] forced_tool={forced_tool} args={json.dumps(forced_args, ensure_ascii=False)}")
            transcript_lines.append(f"[turn {turn}] forced_result={forced_summary[:4000]}")
            if len(tool_history) >= 2 and tool_history[-1] == tool_history[-2] == forced_tool:
                tool_history = tool_history[-1:]
            tool_history.append(forced_tool)
            continue

        if _looks_like_placeholder_response(final_candidate):
            report = _build_fallback_report(topic=topic, scratchpad=scratchpad, candidate=final_candidate)
        else:
            report = final_candidate
        report = _ensure_sources_in_text(report, scratchpad)
        transcript_lines.append(f"[final] chars={len(report)}")
        return ResearchLoopResult(report=report, transcript="\n\n".join(transcript_lines), decision_log=decision_log)

    report = _build_fallback_report(topic=topic, scratchpad=scratchpad, candidate=final_candidate)
    report = _ensure_sources_in_text(report, scratchpad)
    transcript_lines.append(f"[final_fallback] chars={len(report)}")
    return ResearchLoopResult(report=report, transcript="\n\n".join(transcript_lines), decision_log=decision_log)


def _build_thinking_prompt(
    *,
    topic: str,
    source: str,
    loop_label: str,
    turn: int,
    target_turns: int,
    registry: ToolRegistry,
    scratchpad: list[str],
) -> str:
    if scratchpad:
        recent = scratchpad[-4:]
        trimmed_recent = [item[:1200] for item in recent]
        observation = "\n\n".join(trimmed_recent)
    else:
        observation = "(ツール結果なし)"
    policy_lines = [
        "- 出力はJSONのみ",
        "- 形式1: {\"action\":\"tool\",\"tool\":\"...\",\"args\":{...},\"reason\":\"...\"}",
        "- 形式2: {\"action\":\"respond\",\"response\":\"...\"}",
        "- respond は最終ターン以外では避け、必要な場合も追加ツールを優先する",
        "- tool を選ぶときは具体的な引数を与える",
        "- dispatch_research_job は再帰委譲になるので選ばず、必要なら source_deep_dive を使う",
    ]
    if turn < target_turns:
        policy_lines.append(f"- ターン {turn}/{target_turns}: 中盤。できるだけツールを使う")
    else:
        policy_lines.append(f"- ターン {turn}/{target_turns}: 最終ターン。respond で終了してよい")

    return (
        f"[Loop] {loop_label}\n"
        f"[Research Topic]\n{topic}\n\n"
        f"[Source Hint]\n{source}\n\n"
        f"[Available Tools]\n{registry.render_catalog()}\n\n"
        f"[Policy]\n" + "\n".join(policy_lines) + "\n\n"
        f"[Observed Results]\n{observation}"
    )


def _normalize_tool_call(
    *,
    topic: str,
    source: str,
    tool_name: str,
    decision: dict[str, Any],
    turn: int,
    scratchpad: list[str],
) -> tuple[str, dict[str, Any]]:
    if tool_name == "dispatch_research_job":
        tool_name = "source_deep_dive"

    args = decision.get("args", {})
    if not isinstance(args, dict):
        args = {}

    if len(scratchpad) >= 2 and scratchpad[-1] == scratchpad[-2] == tool_name:
        alt_tool, alt_args = _select_forced_tool(topic=topic, turn=turn, scratchpad=scratchpad)
        if alt_tool != tool_name:
            logger.info("Tool repetition guard: %s -> %s", tool_name, alt_tool)
            return alt_tool, alt_args

    if tool_name == "read_url_markdown":
        url = str(args.get("url", "") or "").strip()
        if not url:
            urls = _collect_source_urls(scratchpad)
            if urls:
                args = {"url": urls[0]}
            else:
                tool_name = "web_search"
                args = {"query": f"{topic} official benchmark performance adoption"}

    if tool_name == "web_search" and "query" not in args:
        args = {"query": f"{topic} official benchmark performance adoption"}
    if tool_name == "source_deep_dive":
        args = {
            "topic": str(args.get("topic", "") or topic).strip() or topic,
            "source": str(args.get("source", "") or source).strip() or source,
        }
    return tool_name, args


def _summarize_tool_result(result: str, tool_name: str) -> str:
    if not result:
        return f"[ツール結果: {tool_name}] (空)"
    urls = _extract_urls_from_result(result)
    source_block = ""
    if urls:
        source_block = "\n[出典/参考URL]\n" + "\n".join(f"- {url}" for url in urls)
    limit = 2400 if tool_name == "source_deep_dive" else 800
    return f"[ツール結果: {tool_name}]\n{result[:limit]}{source_block}"


def _select_forced_tool(topic: str, turn: int, scratchpad: list[str]) -> tuple[str, dict[str, Any]]:
    urls = _collect_source_urls(scratchpad)
    mode = turn % 3
    if mode == 1:
        return "web_search", {"query": f"{topic} official benchmark performance adoption"}
    if mode == 2 and urls:
        return "read_url_markdown", {"url": urls[0]}
    return "source_deep_dive", {"topic": topic, "source": "auto"}


def _extract_urls_from_result(result: str) -> list[str]:
    if not result:
        return []
    url_pattern = r'https?://[^\s\n\]"]+'
    urls = re.findall(url_pattern, result)
    seen: set[str] = set()
    unique_urls: list[str] = []
    for url in urls:
        clean = _normalize_extracted_url(url)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        unique_urls.append(clean)
    return unique_urls[:5]


def _collect_source_urls(scratchpad: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in scratchpad:
        for url in _extract_urls_from_result(item):
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
    return out


def _ensure_sources_in_text(text: str, scratchpad: list[str]) -> str:
    body = (text or "").strip()
    if not body:
        body = "(調査結果の生成に失敗しました)"

    urls = _collect_source_urls(scratchpad)
    if not urls:
        return body + "\n\n[参考URL]\n- 取得できませんでした"
    if "参考URL" in body or "[参考にした情報源]" in body:
        return body

    source_lines = "\n".join(f"- {u}" for u in urls[:12])
    return f"{body}\n\n[参考URL]\n{source_lines}"


def _looks_like_placeholder_response(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    placeholders = {
        "(タイムアウト)",
        "(解析エラー)",
        "(レスポンス生成失敗)",
        "(調査結果の生成に失敗しました)",
    }
    if t in placeholders:
        return True
    return len(t) <= 16 and t.startswith("(") and t.endswith(")")


def _build_fallback_report(topic: str, scratchpad: list[str], candidate: str = "") -> str:
    urls = _collect_source_urls(scratchpad)
    tool_blocks = [s for s in scratchpad if s.startswith("[ツール結果:")]
    lines = [
        "調査を実施しましたが、最終整形を自動生成できなかったため、収集結果を要約して返します。",
        "",
        "[トピック]",
        topic,
        "",
        "[収集結果サマリ]",
    ]
    if candidate and not _looks_like_placeholder_response(candidate):
        lines.extend([candidate, ""])
    if tool_blocks:
        for idx, block in enumerate(tool_blocks[:6], start=1):
            lines.append(f"{idx}. {block[:280].replace(chr(10) + chr(10), chr(10))}")
    else:
        lines.append("- 有効なツール結果が取得できませんでした。")
    lines.extend(["", "[参考URL]"])
    if urls:
        lines.extend([f"- {u}" for u in urls[:12]])
    else:
        lines.append("- 取得できませんでした")
    return "\n".join(lines).strip()


def _normalize_extracted_url(url: str) -> str:
    clean = (url or "").strip()
    if not clean:
        return ""
    for marker in ("\\n", "/n", "\n"):
        idx = clean.find(marker)
        if idx >= 0:
            clean = clean[:idx]
    clean = clean.rstrip('.,;)-')
    if not clean.startswith("http://") and not clean.startswith("https://"):
        return ""
    return clean


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None

    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?", "", candidate).strip()
        candidate = re.sub(r"```$", "", candidate).strip()

    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    match = re.search(r"\{[\s\S]*\}", candidate)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None
