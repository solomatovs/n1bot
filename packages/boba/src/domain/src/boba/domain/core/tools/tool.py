"""Базовый класс :class:`Tool` и связанные value-объекты вызова."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from boba.domain.core.patterns import Converter, Definition, Executor
from boba.domain.core.tools.schema import (
    ToolDefinition,
    ToolId,
    ToolSourceId,
)
from boba.domain.core.tools.validators import SchemaArgsValidator
from boba.domain.core.workspace import ProjectWorkspaceShell

TArgs = TypeVar("TArgs")


@dataclass(frozen=True)
class ToolContext:
    """Per-request контекст исполнения tool'а.

    Tool-классы — application-singletons (создаются один раз при
    регистрации плагина и кэшируются в :class:`ToolsService`). Всё, что
    зависит от конкретной user-сессии — проходит через ``ToolContext``,
    который middleware строит на каждый запрос и пробрасывает в
    :meth:`Tool.execute`.

    Сейчас контракт минимальный — только ``project_workspace``. Если
    появится tool, которому нужны другие per-request зависимости (LLM-
    клиент, секреты, agent-config), они добавятся сюда; контракт
    расширяется по мере появления реальных потребителей.
    """

    project_workspace: ProjectWorkspaceShell


@dataclass(frozen=True)
class ToolCall:
    """Запрос на вызов инструмента.

    ``tool_id`` — к какому инструменту;
    ``arguments`` — сырой dict, каким его сформировал caller (в случае
    LLM это то, что модель сериализовала в JSON).
    """

    tool_id: ToolId
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """Результат успешного выполнения инструмента.

    Ошибки не представляются отдельным флагом — они бросаются как
    :class:`ToolExecutionError` и обрабатываются caller-ом (в agent-слое
    middleware превращает их в tool-сообщение для LLM + наблюдательное
    событие).
    """

    content: str


class Tool(
    Executor[ToolContext, TArgs, ToolResult],
    Definition[ToolDefinition],
    Generic[TArgs],
):
    """Базовый класс tool'а.

    Шаблонный метод :meth:`args_converter` собирает pipeline:
    :class:`SchemaArgsValidator` (валидация по схеме) →
    :meth:`typed_args_converter` (маппинг провалидированного dict в
    типизированный TArgs). Перекрывать :meth:`args_converter` нельзя —
    валидация по схеме обязательна для всех tool'ов.

    ``execute(ctx: ToolContext, args: TArgs)`` принимает per-request
    контекст с ``project_workspace`` и любым DI, зависящим от сессии.
    Сами Tool-инстансы — application-singletons: создаются один раз при
    регистрации плагина и переиспользуются между запросами; никакого
    per-session состояния в ``self`` хранить нельзя.
    """

    @abstractmethod
    def tool_id(self) -> ToolId: ...

    @abstractmethod
    def tool_source_id(self) -> ToolSourceId: ...

    @abstractmethod
    def typed_args_converter(self) -> Converter[dict[str, Any], TArgs]:
        """Маппер провалидированного dict в типизированный TArgs.

        На вход приходит dict, уже прошедший
        :class:`SchemaArgsValidator`: только известные ключи, типы
        соответствуют схеме, default'ы применены. Реализация просто
        собирает dataclass из готовых полей — без проверок и
        приведения типов.
        """
        ...

    def args_converter(self) -> Converter[dict[str, Any], TArgs]:
        """Pipeline: валидация по схеме → маппинг в TArgs.

        Метод финален de facto — переопределять не нужно: логика
        полностью выводится из :meth:`definition` и
        :meth:`typed_args_converter`.
        """
        return _ToolArgsPipeline(
            SchemaArgsValidator(self.definition().input_schema, self.tool_id()),
            self.typed_args_converter(),
        )


class _ToolArgsPipeline(Converter[dict[str, Any], TArgs], Generic[TArgs]):
    """Pipeline валидации + маппинга для :meth:`Tool.args_converter`.

    :class:`InvalidToolArgumentError` (`ToolExecutionError`-потомок)
    пропускается наружу — caller (agent middleware) ловит его в
    `except ToolExecutionError` и декларирует `ToolResultEffect`
    с текстом ошибки для LLM. Контракт `Converter` про
    `ConverterError` нарушается осознанно: для tool args
    домен-специфичная иерархия `ToolExecutionError` — единый канал
    ошибок.
    """

    def __init__(
        self,
        validator: SchemaArgsValidator,
        typed: Converter[dict[str, Any], TArgs],
    ) -> None:
        self._validator = validator
        self._typed = typed

    def convert(self, value: dict[str, Any]) -> TArgs:
        validated = self._validator.convert(value)
        return self._typed.convert(validated)
