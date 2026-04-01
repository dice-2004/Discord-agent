from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from discord_ai_agent.tools.action_tools import execute_internal_action
from discord_ai_agent.tools.cli_tools import run_local_cli
from discord_ai_agent.tools.deep_dive_tools import source_deep_dive
from discord_ai_agent.tools.research_tools import dispatch_research_job, get_research_job_status
from discord_ai_agent.tools.reader_tools import read_url_markdown
from discord_ai_agent.tools.search_tools import web_search


ToolFunc = Callable[..., str]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    args_schema: dict[str, str]
    required_args: list[str] | None
    func: ToolFunc


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec]) -> None:
        self._specs = {spec.name: spec for spec in specs}

    def list_specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def render_catalog(self) -> str:
        lines: list[str] = []
        for spec in self.list_specs():
            arg_lines = ", ".join(f"{k}:{v}" for k, v in spec.args_schema.items())
            required = spec.required_args or list(spec.args_schema.keys())
            lines.append(
                f"- {spec.name}: {spec.description} | args: {arg_lines} | required: {','.join(required)}"
            )
        return "\n".join(lines)

    def tool_names(self) -> set[str]:
        return set(self._specs.keys())

    def execute(self, tool_name: str, args: dict[str, Any]) -> str:
        spec = self._specs.get(tool_name)
        if spec is None:
            return f"未知のツールです: {tool_name}"

        normalized_args, error = self._normalize_args(spec, args)
        if error is not None:
            return f"引数不正: {error}"

        try:
            return spec.func(**normalized_args)
        except TypeError as exc:
            return f"引数不正: {exc}"
        except Exception as exc:
            return f"ツール実行失敗: {exc}"

    def _normalize_args(self, spec: ToolSpec, args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        if not isinstance(args, dict):
            return {}, "argsはオブジェクト形式で指定してください"

        # Compatibility shim for model output drift when calling execute_internal_action.
        # Accept parameters/payload dict and convert to payload_json.
        if spec.name == "execute_internal_action":
            working = dict(args)
            if "payload_json" not in working:
                for alt_key in ("parameters", "payload", "params"):
                    candidate = working.get(alt_key)
                    if isinstance(candidate, dict):
                        working["payload_json"] = json.dumps(candidate, ensure_ascii=False)
                        break
            elif isinstance(working.get("payload_json"), dict):
                working["payload_json"] = json.dumps(working["payload_json"], ensure_ascii=False)
            args = working

        normalized: dict[str, Any] = {}
        required = spec.required_args or list(spec.args_schema.keys())

        for key in required:
            if key not in args:
                return {}, f"必須引数 '{key}' が不足しています"

        for key, type_desc in spec.args_schema.items():
            if key not in args:
                continue

            value = args.get(key)
            if type_desc.startswith("string"):
                if value is None:
                    return {}, f"引数 '{key}' は文字列が必要です"
                value = str(value)

            if key == "source":
                allowed = {"auto", "github", "reddit", "youtube", "x"}
                source_value = str(value).strip().lower()
                if source_value not in allowed:
                    return {}, f"sourceは {sorted(allowed)} のいずれかを指定してください"
                value = source_value

            normalized[key] = value

        return normalized, None


def build_default_tool_registry() -> ToolRegistry:
    specs = [
        ToolSpec(
            name="web_search",
            description="一般Web検索で候補情報を集める",
            args_schema={"query": "string"},
            required_args=["query"],
            func=web_search,
        ),
        ToolSpec(
            name="read_url_markdown",
            description="URL本文をMarkdownとして取得する",
            args_schema={"url": "string"},
            required_args=["url"],
            func=read_url_markdown,
        ),
        ToolSpec(
            name="source_deep_dive",
            description="GitHub/Reddit/X/YouTubeなど特定ソースを深掘りする",
            args_schema={"topic": "string", "source": "string(auto/github/reddit/youtube/x)"},
            required_args=["topic"],
            func=source_deep_dive,
        ),
        ToolSpec(
            name="dispatch_research_job",
            description="Research Agentへ重い調査ジョブを委譲する",
            args_schema={
                "topic": "string",
                "source": "string(auto/github/reddit/youtube/x)",
                "wait": "string(true/false)",
                "mode": "string(auto/gemini_cli/fallback)",
                "timeout_sec": "string(int seconds)",
            },
            required_args=["topic"],
            func=dispatch_research_job,
        ),
        ToolSpec(
            name="get_research_job_status",
            description="Research Agentジョブの現在状態を取得する",
            args_schema={"job_id": "string"},
            required_args=["job_id"],
            func=get_research_job_status,
        ),
        ToolSpec(
            name="run_local_cli",
            description="承認トークン付きで許可コマンドのみ実行する",
            args_schema={"command": "string", "approval_token": "string"},
            required_args=["command", "approval_token"],
            func=run_local_cli,
        ),
        ToolSpec(
            name="execute_internal_action",
            description="許可済みactionをコード内で直接実行する",
            args_schema={"action": "string", "payload_json": "string(JSON object)"},
            required_args=["action"],
            func=execute_internal_action,
        ),
    ]
    return ToolRegistry(specs)
