from __future__ import annotations

import base64
import json
import os
import re
from urllib.request import Request, urlopen

from tools.ai_exchange_logger import log_ai_exchange
from tools.search_tools import web_search


def _safe_int(env_key: str, default_value: int) -> int:
    raw = os.getenv(env_key, str(default_value)).strip()
    try:
        return int(raw)
    except ValueError:
        return default_value


def source_deep_dive(topic: str, source: str = "auto") -> str:
    """特定ソースを深掘りするための検索クエリ束を生成して実行する。"""
    clean_topic = (topic or "").strip()
    clean_source = (source or "auto").strip().lower()
    if not clean_topic:
        return "deep dive対象のtopicが空です。"

    query_plan = _build_query_plan(clean_topic, clean_source)
    max_queries = _safe_int("DEEP_DIVE_MAX_QUERIES", 3)
    query_plan = _dedupe_queries(query_plan)[:max_queries]

    outputs: list[str] = []
    repo = _extract_github_repo(clean_topic)
    if repo is not None and clean_source in {"github", "auto"}:
        outputs.append(_probe_github_repo(repo[0], repo[1]))

    for idx, query in enumerate(query_plan, start=1):
        result = web_search(query)
        log_ai_exchange(
            component="deepdive",
            model="web_search",
            prompt=query,
            response=result,
            metadata={
                "phase": "deep_dive_query",
                "query_index": idx,
                "source": clean_source,
            },
        )
        outputs.append(f"[DeepDive Query {idx}] {query}\n{result}")
        if "レート制限" in result:
            outputs.append("[DeepDive] レート制限を検知したため、残りクエリはスキップしました。")
            break

    merged = "\n\n".join(outputs)
    if len(merged) > 7000:
        merged = merged[:7000] + "..."
    return merged


def _build_query_plan(topic: str, source: str) -> list[str]:
    if source == "github":
        return [
            f"site:github.com {topic} issue",
            f"site:github.com {topic} release notes",
            f"site:github.com {topic} discussion",
        ]
    if source == "reddit":
        return [
            f"site:reddit.com {topic}",
            f"site:reddit.com {topic} latest",
            f"site:reddit.com {topic} troubleshooting",
        ]
    if source == "youtube":
        return [
            f"site:youtube.com {topic}",
            f"site:youtube.com {topic} review",
            f"site:youtube.com {topic} tutorial",
        ]
    if source == "x":
        return [
            f"site:x.com {topic}",
            f"site:x.com {topic} latest",
            f"site:x.com {topic} opinion",
        ]

    return [
        topic,
        f"{topic} official",
        f"{topic} latest updates",
    ]


def _dedupe_queries(queries: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for query in queries:
        normalized = " ".join((query or "").strip().lower().split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(query)
    return unique


def _extract_github_repo(topic: str) -> tuple[str, str] | None:
    text = (topic or "").strip()
    if not text:
        return None

    m_url = re.search(r"github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", text)
    if m_url:
        return m_url.group(1), m_url.group(2)

    m_short = re.search(r"\b([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\b", text)
    if m_short:
        return m_short.group(1), m_short.group(2)
    return None


def _probe_github_repo(owner: str, repo: str) -> str:
    base = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "discord-ai-agent/1.0",
    }

    def _fetch(url: str, timeout: int = 10) -> tuple[dict[str, object] | None, int]:
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=timeout) as res:
                code = int(getattr(res, "status", 200))
                payload = json.loads(res.read().decode("utf-8", errors="replace") or "{}")
                if not isinstance(payload, dict):
                    return None, code
                return payload, code
        except Exception:
            return None, 0

    def _fetch_list(url: str, timeout: int = 10) -> tuple[list[dict[str, object]], int]:
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=timeout) as res:
                code = int(getattr(res, "status", 200))
                payload = json.loads(res.read().decode("utf-8", errors="replace") or "[]")
                if not isinstance(payload, list):
                    return [], code
                rows = [row for row in payload if isinstance(row, dict)]
                return rows, code
        except Exception:
            return [], 0

    repo_payload, repo_status = _fetch(base)
    if repo_payload is None:
        return (
            "[GitHub Repo Probe]\n"
            f"repo: {owner}/{repo}\n"
            f"status: unavailable ({repo_status or 'network_error'})"
        )

    readme_payload, readme_status = _fetch(f"{base}/readme")
    readme_text = "unknown"
    readme_url = ""
    readme_headline = ""
    readme_excerpt = ""
    readme_contains_kc3hack = "unknown"
    if readme_payload is not None:
        readme_text = "found"
        readme_url = str(readme_payload.get("html_url", "") or "")
        readme_body = _decode_readme_text(readme_payload)
        readme_headline = _extract_readme_headline(readme_body)
        readme_excerpt = _extract_readme_excerpt(readme_body)
        readme_contains_kc3hack = "yes" if "kc3hack" in readme_body.lower() else "no"
    elif readme_status == 404:
        readme_text = "not_found"
        readme_contains_kc3hack = "no"
    else:
        readme_text = f"unavailable ({readme_status or 'network_error'})"

    description = str(repo_payload.get("description", "") or "").strip()
    default_branch = str(repo_payload.get("default_branch", "") or "")
    stars = int(repo_payload.get("stargazers_count", 0) or 0)
    about_contains_kc3hack = "yes" if "kc3hack" in description.lower() else "no"
    issue_rows, _ = _fetch_list(f"{base}/issues?state=all&per_page=30")
    open_issue_count = 0
    open_pr_count = 0
    latest_issue_title = ""
    latest_issue_url = ""
    latest_issue_updated = ""
    latest_pr_title = ""
    latest_pr_url = ""
    latest_pr_updated = ""

    for row in issue_rows:
        is_pr = isinstance(row.get("pull_request"), dict)
        state = str(row.get("state", "") or "").strip().lower()
        updated_at = str(row.get("updated_at", "") or "").strip()
        title = str(row.get("title", "") or "").strip()
        html_url = str(row.get("html_url", "") or "").strip()

        if is_pr:
            if state == "open":
                open_pr_count += 1
            if not latest_pr_updated or updated_at > latest_pr_updated:
                latest_pr_updated = updated_at
                latest_pr_title = title[:180]
                latest_pr_url = html_url
        else:
            if state == "open":
                open_issue_count += 1
            if not latest_issue_updated or updated_at > latest_issue_updated:
                latest_issue_updated = updated_at
                latest_issue_title = title[:180]
                latest_issue_url = html_url

    lines = [
        "[GitHub Repo Probe]",
        f"repo: {owner}/{repo}",
        f"default_branch: {default_branch or '(unknown)'}",
        f"stars: {stars}",
        f"open_issues: {open_issue_count}",
        f"open_prs: {open_pr_count}",
        f"about_description: {description[:220] if description else '(none)'}",
        f"about_contains_kc3hack: {about_contains_kc3hack}",
        f"README: {readme_text}",
        f"README_contains_kc3hack: {readme_contains_kc3hack}",
    ]
    if readme_url:
        lines.append(f"README_URL: {readme_url}")
    if readme_headline:
        lines.append(f"README_headline: {readme_headline}")
    if readme_excerpt:
        lines.append(f"README_excerpt: {readme_excerpt}")
    if latest_issue_title:
        lines.append(f"latest_issue_title: {latest_issue_title}")
    if latest_issue_url:
        lines.append(f"latest_issue_url: {latest_issue_url}")
    if latest_issue_updated:
        lines.append(f"latest_issue_updated_at: {latest_issue_updated}")
    if latest_pr_title:
        lines.append(f"latest_pr_title: {latest_pr_title}")
    if latest_pr_url:
        lines.append(f"latest_pr_url: {latest_pr_url}")
    if latest_pr_updated:
        lines.append(f"latest_pr_updated_at: {latest_pr_updated}")
    lines.append("note: about_description はリポジトリAbout欄、README_headline はREADME本文由来")
    return "\n".join(lines)


def _decode_readme_text(readme_payload: dict[str, object]) -> str:
    content = str(readme_payload.get("content", "") or "")
    encoding = str(readme_payload.get("encoding", "") or "").lower()
    if not content or encoding != "base64":
        return ""
    try:
        return base64.b64decode(content, validate=False).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_readme_headline(decoded_readme_text: str) -> str:
    lines = [ln.strip() for ln in (decoded_readme_text or "").splitlines() if ln.strip()]
    for line in lines:
        if line.startswith("#"):
            text = line.lstrip("#").strip()
        else:
            text = line
        text = re.sub(r"`([^`]*)`", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
        if text:
            return text[:220]
    return ""


def _extract_readme_excerpt(decoded_readme_text: str) -> str:
    lines = [ln.strip() for ln in (decoded_readme_text or "").splitlines() if ln.strip()]
    snippets: list[str] = []
    for line in lines:
        text = line.lstrip("#").strip()
        text = re.sub(r"`([^`]*)`", r"\1", text)
        text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
        if not text:
            continue
        snippets.append(text)
        if len(snippets) >= 3:
            break
    excerpt = " / ".join(snippets)
    return excerpt[:260]
