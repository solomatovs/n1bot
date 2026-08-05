"""Операции над графиками: валидация Plotly-спеки внутри песочницы."""

from __future__ import annotations

import json
import sys
from typing import Any, ClassVar

from plotly import graph_objects as go

from boba.toolkit.payload import ChunkEmitter, PayloadEntry


class ChartOps:
    """Операции над figure-спекой; вызываются диспетчером payload'а."""

    OPS: ClassVar[tuple[str, ...]] = ("validate_figure",)

    @classmethod
    async def dispatch(
        cls, request: dict[str, Any], emit: ChunkEmitter
    ) -> dict[str, Any]:
        op = request["op"]
        if op == "validate_figure":
            return cls.validate_figure(request)
        msg = f"unknown chart op: {op!r}"
        raise ValueError(msg)

    @classmethod
    def validate_figure(cls, request: dict[str, Any]) -> dict[str, Any]:
        """Проверить спеку схемой plotly и достать заголовок."""
        try:
            parsed = json.loads(request["spec"])
        except json.JSONDecodeError as e:
            msg = f"spec is not valid JSON: {e}"
            raise ValueError(msg) from e
        if not isinstance(parsed, dict):
            msg = f"spec must be a JSON figure object, got {type(parsed).__name__}"
            raise ValueError(msg)
        try:
            go.Figure(parsed)
        except (ValueError, TypeError) as e:
            msg = f"invalid Plotly figure spec: {e}"
            raise ValueError(msg) from e
        return {"title": cls.title_of(parsed)}

    @staticmethod
    def title_of(parsed: dict[str, Any]) -> str:
        """layout.title бывает и строкой, и объектом с полем text."""
        layout = parsed.get("layout")
        if not isinstance(layout, dict):
            return ""
        raw = layout.get("title")
        if isinstance(raw, dict):
            raw = raw.get("text")
        if not raw:
            return ""
        return str(raw)


if __name__ == "__main__":
    sys.exit(PayloadEntry.main(ChartOps.dispatch))
