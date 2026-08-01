"""Кодек artifact'а инструмента: dict из checkpointer'а -> ToolResult."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, cast, get_args

from pydantic import TypeAdapter

from boba.chainlit2.rendering.tool_result import ToolResult, ToolResultBase

__all__ = ["ToolArtifact"]


class ToolArtifact:
    """Поднимает artifact в модель: langgraph сериализует его в обычный dict."""

    _ADAPTER: ClassVar[TypeAdapter[ToolResult]] = TypeAdapter(ToolResult)
    _KINDS: ClassVar[frozenset[str]] = frozenset(
        get_args(variant.model_fields["kind"].annotation)[0]
        for variant in get_args(get_args(ToolResult)[0])
    )

    @classmethod
    def revive(cls, artifact: Any) -> ToolResult | None:
        if isinstance(artifact, ToolResultBase):
            return cast(ToolResult, artifact)
        if isinstance(artifact, Mapping) and artifact.get("kind") in cls._KINDS:
            return cls._ADAPTER.validate_python(dict(artifact))
        return None
