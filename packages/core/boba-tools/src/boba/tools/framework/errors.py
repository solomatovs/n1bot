"""Ошибки регистрации framework-слоя: коллизии имён/источников.

Возникают при сборке StaticToolSource/ToolRegistry, а не в runtime
вызова tool'а (те живут в boba.tools.domain.errors). Поэтому наследуют
Exception напрямую, а не ToolExecutionError.
"""

from __future__ import annotations

from boba.tools.domain.ids import ToolName, ToolSourceId

__all__ = [
    "ToolIdCollisionError",
    "ToolNameCollisionError",
    "ToolSourceCollisionError",
]


class ToolIdCollisionError(Exception):
    """Внутри одного source — два tool'а с одинаковым ToolName."""

    def __init__(self, source_id: ToolSourceId, name: ToolName) -> None:
        super().__init__(
            f"source {source_id!r} declares tool {name!r} more than once",
        )
        self.source_id = source_id
        self.name = name


class ToolNameCollisionError(Exception):
    """Два source'а объявляют tool с одинаковым wire-именем ToolName.

    Wire-имя = идентификатор маршрутизации; глобальный дубль сделал бы вызов
    неоднозначным, поэтому реестр падает на сборке.
    """

    def __init__(
        self,
        name: ToolName,
        existing_source: ToolSourceId,
        new_source: ToolSourceId,
    ) -> None:
        super().__init__(
            f"tool name {name!r} declared by both source "
            f"{existing_source!r} and {new_source!r}",
        )
        self.name = name
        self.existing_source = existing_source
        self.new_source = new_source


class ToolSourceCollisionError(Exception):
    """Два source'а с одинаковым ToolSourceId в одном ToolRegistry."""

    def __init__(self, source_id: ToolSourceId) -> None:
        super().__init__(f"duplicate tool source {source_id!r}")
        self.source_id = source_id
