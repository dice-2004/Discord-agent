"""Shared tool package for Discord AI agent (Main and Research).

Keep this module side-effect free so submodule imports like
`tools.ai_exchange_logger` do not pull heavy optional dependencies.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ToolRegistry", "build_default_tool_registry"]


def __getattr__(name: str) -> Any:
    if name in {"ToolRegistry", "build_default_tool_registry"}:
        from tools.tool_registry import ToolRegistry, build_default_tool_registry

        return {"ToolRegistry": ToolRegistry, "build_default_tool_registry": build_default_tool_registry}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
