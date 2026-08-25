"""Каталог инструментов для workflow: что домен знает о реестре под субъекта.

Доступность — решение ToolAccess под роли и профиль субъекта. Аргументы — из
LLM-схемы инструмента, без служебного intent. Порты появятся с потоками;
пока портов ни у кого нет.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from boba.toolkit.calls import ToolIntent
from boba.workflow import ToolArg, ToolCatalog, ToolFacts

if TYPE_CHECKING:
    from boba.chainlit.infra.plugins import ToolRegistry

__all__ = ["CatalogBuilder"]


class CatalogBuilder:
    """Собирает ToolCatalog из реестра приложения под роли и профиль."""

    @classmethod
    def of(
        cls, registry: ToolRegistry, roles: Iterable[str], profile: str
    ) -> ToolCatalog:
        user_roles = frozenset(roles)

        catalog: dict[str, ToolFacts] = {}
        for tool in registry.tools:
            catalog[tool.name] = cls._facts(registry, tool, user_roles, profile)

        return catalog

    @classmethod
    def _facts(
        cls,
        registry: ToolRegistry,
        tool: BaseTool,
        roles: frozenset[str],
        profile: str,
    ) -> ToolFacts:
        return ToolFacts(
            name=tool.name,
            availability=registry.access.decide(tool.name, roles, profile),
            args=tuple(cls._args(tool)),
        )

    @staticmethod
    def _args(tool: BaseTool) -> Iterator[ToolArg]:
        """Аргументы, которые видит модель; intent задача задаёт по желанию."""
        schema = tool.tool_call_schema
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            return

        for name, field in schema.model_fields.items():
            if name == ToolIntent.NAME:
                yield ToolArg(name=name, required=False)
                continue

            yield ToolArg(name=name, required=field.is_required())
