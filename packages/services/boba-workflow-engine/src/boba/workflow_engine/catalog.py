"""Каталог инструментов для workflow: что домен знает о реестре под субъекта.

Доступность — решение ToolAccess под роли и профиль субъекта. Аргументы — из
LLM-схемы инструмента с видом по объявлению у поля (ArgViews); виды
результата — из Produces в аннотации возвращаемого типа. Порты появятся с
потоками; пока портов ни у кого нет.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Iterator
from typing import Annotated, Any, get_args, get_origin

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from boba.toolkit.calls import ArgViews, ToolCallViews
from boba.toolkit.result import Produces
from boba.toolrun.registry import ToolRegistry
from boba.workflow import ToolArg, ToolCatalog, ToolFacts

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
            description=cls._summary(tool.description),
            args=tuple(cls._args(tool)),
            results=cls._results(tool),
        )

    @staticmethod
    def _summary(description: str) -> str:
        first_line = description.strip().split("\n")[0]
        return first_line.strip()

    @staticmethod
    def _args(tool: BaseTool) -> Iterator[ToolArg]:
        """Аргументы, которые видит модель; intent задача задаёт по желанию."""
        schema = tool.tool_call_schema
        if not isinstance(schema, type) or not issubclass(schema, BaseModel):
            return

        call = ToolCallViews.of(tool.name)
        for name, field in schema.model_fields.items():
            view = ArgViews.of_field(name, field, call)
            description = field.description
            if description is None:
                description = ""

            yield ToolArg(
                name=name,
                required=field.is_required(),
                view=view,
                description=description,
            )

    @classmethod
    def _results(cls, tool: BaseTool) -> tuple[str, ...]:
        """Виды результата из Produces в Annotated возвращаемого типа тела."""
        body = cls._body(tool)
        if body is None:
            return ()

        # тела с `from __future__ import annotations` держат аннотацию строкой
        try:
            returns = inspect.signature(body, eval_str=True).return_annotation
        except (NameError, TypeError, ValueError):
            return ()

        if get_origin(returns) is not Annotated:
            return ()

        for item in get_args(returns)[1:]:
            if isinstance(item, Produces):
                return item.kinds

        return ()

    @staticmethod
    def _body(tool: BaseTool) -> Callable[..., Any] | None:
        coroutine = getattr(tool, "coroutine", None)
        if coroutine is not None:
            return coroutine

        func = getattr(tool, "func", None)
        if func is not None:
            return func

        return None
