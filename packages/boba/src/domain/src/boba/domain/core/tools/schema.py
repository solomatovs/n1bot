"""Идентификаторы tool-домена.

Описание tool'а — это :class:`~boba.domain.core.declaration.ObjectSchema`
напрямую (одно и то же примитив, что описывает config-секцию). Никаких
обёрток вокруг ObjectSchema нет: ``ToolDefinition``/``ToolInputSchema``
дублировали бы существующее API без новой пользы.

Tool возвращает свой :class:`ObjectSchema` через :meth:`Tool.definition`;
``description`` живёт на самой схеме (``ObjectSchema.description``).
"""

from __future__ import annotations

from typing import Self

from boba.domain.core.patterns import Id


class ToolId(Id[str]):
    """Уникальный идентификатор инструмента — сквозной ключ поиска и вызова."""

    def to_wire(self) -> str:
        return self._name

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(value)


class ToolSourceId(Id[str]):
    """Идентификатор источника инструментов (builtin, mcp:server_a, ...)."""

    def to_wire(self) -> str:
        return self._name

    @classmethod
    def from_wire(cls, value: str) -> Self:
        return cls(value)
