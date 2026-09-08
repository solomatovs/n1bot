"""Каталог инструментов для workflow: что домен знает о реестре под субъекта.

Доступность — решение ToolAccess под роли и профиль субъекта. Аргументы —
форма модели вызова (ToolCallBase.studio_view) без скрытых полей; виды
результата — из аннотации возвращаемого типа тела. Порты появятся с
потоками; пока портов ни у кого нет.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from langchain_core.tools import BaseTool

from boba.toolkit.calls import FieldPlacement, StudioField, ToolCallBase
from boba.toolkit.ports import PortDirection as StreamDirection
from boba.toolkit.ports import ToolStreamSpecs
from boba.toolkit.result import ResultKindError, ResultKinds
from boba.toolrun.registry import ToolRegistry
from boba.toolrun.wrapping import ToolSchema
from boba.workflow import ToolCatalog, ToolFacts, ToolPort
from boba.workflow.spec import PortDirection

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
            ports=cls._ports(tool.name),
            results=cls._results(tool),
        )

    @staticmethod
    def _ports(name: str) -> tuple[ToolPort, ...]:
        """fd-порты из потоковой декларации: inbound читает ребро, outbound пишет."""
        ports: list[ToolPort] = []
        for decl in ToolStreamSpecs.of(name).ports:
            if decl.direction is StreamDirection.INBOUND:
                direction = PortDirection.READ
            else:
                direction = PortDirection.WRITE

            ports.append(ToolPort(name=decl.name, direction=direction))

        return tuple(ports)

    @staticmethod
    def _summary(description: str) -> str:
        first_line = description.strip().split("\n")[0]
        return first_line.strip()

    @staticmethod
    def _args(tool: BaseTool) -> Iterator[StudioField]:
        """Поля формы, которые видит модель: скрытые (injected, порты) не в счёт;
        intent задача задаёт по желанию."""
        schema = ToolSchema.of(tool)
        if schema is None:
            return

        if not issubclass(schema, ToolCallBase):
            return

        for field in schema.studio_view().fields:
            if field.placement is FieldPlacement.HIDDEN:
                continue

            yield field

    @classmethod
    def _results(cls, tool: BaseTool) -> tuple[str, ...]:
        """Виды результата из аннотации возврата тела: класс либо union."""
        body = cls._body(tool)
        if body is None:
            return ()

        # тела с `from __future__ import annotations` держат аннотацию строкой
        try:
            returns = inspect.signature(body, eval_str=True).return_annotation
        except (NameError, TypeError, ValueError):
            return ()

        if returns is inspect.Signature.empty:
            return ()

        try:
            return ResultKinds.kinds_of(returns)
        except ResultKindError:
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
