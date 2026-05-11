"""Идентификаторы tool-домена.

Имя tool'а живёт в **двух пространствах**:

- `ToolName` — локальное имя в пределах своего `ToolSource` (например,
  `outline` внутри `plugin.html`). Уникальность гарантируется самим
  source'ом — глобальной коллизии нет.
- `ToolId` — wire-формат, по которому tool вызывается LLM-ом:
  `<source_id>/<name>`. Единственная точка композиции/парсинга — `ToolExecutor`.
"""

from __future__ import annotations

from typing import Self

from boba.patterns import Id

__all__ = ["ToolId", "ToolName", "ToolSourceId"]

_SEPARATOR = "/"


class ToolName(Id[str]):
    """Локальное имя инструмента внутри своего source'а."""

    def to_wire(self) -> str:
        return self._name

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(value)


class ToolSourceId(Id[str]):
    """Идентификатор источника инструментов (`plugin.html`, `mcp.github`, ...)."""

    def to_wire(self) -> str:
        return self._name

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(value)


class ToolId(Id[str]):
    """Wire-формат: `<source_id>/<tool_name>`. Композируется из пары."""

    def to_wire(self) -> str:
        return self._name

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(value)

    @classmethod
    def compose(cls, source_id: ToolSourceId, name: ToolName) -> Self:
        return cls(f"{source_id.to_wire()}{_SEPARATOR}{name.to_wire()}")

    def parse(self) -> tuple[ToolSourceId, ToolName]:
        """
        `plugin.html/outline` → (ToolSourceId('plugin.html'), ToolName('outline'))
        """
        source_part, sep, name_part = self._name.partition(_SEPARATOR)
        if not sep or not name_part:
            msg = (
                f"invalid qualified tool id {self._name!r}: expected '<source>/<name>'"
            )
            raise ValueError(msg)
        return ToolSourceId(source_part), ToolName(name_part)
