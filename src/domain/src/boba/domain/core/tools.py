"""Generic tool/dispatch framework.

Абстракция callable-инструментов с типизированными аргументами и
результатом; реестр источников, каталог, диспетчер вызовов. Модуль
**не завязан на LLM**: :class:`Tool` — это ``Executor[None, TArgs, ToolResult]``,
а вся LLM-специфика (tool_call_id, ``role="tool"`` сообщения, событие
``ToolExecutionFailed``) живёт в agent-слое, который этим фреймворком
пользуется.

В проекте потребитель ровно один — agent loop для LLM tool calling — но
сам модуль любой другой caller (CQRS-шина, RPC-диспетчер, …) мог бы
использовать без правок.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Generic, Self, TypeVar

from boba.domain.core.patterns import (
    Converter,
    Definition,
    Executor,
    FoldFactory,
    Id,
    PrioritySource,
    Validator,
)
from boba.domain.core.validation import (
    MISSING,
    ParamValidationError,
    ParamWireSchema,
    SchemaContributor,
)

TArgs = TypeVar("TArgs")


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


@dataclass(frozen=True)
class ParamSchema:
    """Описание одного параметра инструмента.

    Все три поля обязательны — сознательно строгая схема: автор tool'а
    обязан явно задать имя, человекочитаемое описание и валидатор
    (даже :class:`Pass` для no-op случая). Тип, ``required``,
    ``default``, ``enum`` и прочие schema-аспекты выводятся из
    ``validator`` через :class:`SchemaContributor` — единый источник
    правды для runtime-валидации и wire-схемы.
    """

    name: str
    description: str
    validator: Validator[Any]


@dataclass(frozen=True)
class ToolInputSchema:
    """Схема входных параметров инструмента.

    ``params`` — независимые описания каждого параметра (валидация +
    описание для LLM).

    ``invariants`` — cross-field валидатор, который работает над dict'ом
    уже провалидированных параметров. Проверяет инварианты, связывающие
    несколько полей: взаимоисключения, совместность, порядок. Обязателен
    даже при отсутствии таких связей — тогда передавай :class:`Pass`.
    """

    params: list[ParamSchema]
    invariants: Validator[dict[str, Any]]


def build_param_wire_schema(param: ParamSchema) -> ParamWireSchema:
    """Собрать wire-описание параметра, обходя валидатор.

    Стартует с ``description`` (всегда есть) и даёт валидатору
    дозаполнить ``type``/``enum``/``default``/``required`` через
    :meth:`SchemaContributor.contribute`. Если валидатор не реализует
    contributor-протокол — вернёт минимум: только описание.
    """
    schema = ParamWireSchema(property={"description": param.description})
    if isinstance(param.validator, SchemaContributor):
        param.validator.contribute(schema)
    return schema


@dataclass(frozen=True)
class ToolDefinition:
    """
    Описание инструмента для потребителя: текст и схема параметров.

    ``id`` намеренно не хранится — он живёт на самом :class:`Tool`
    (:meth:`Tool.tool_id`) и считается источником правды. Запись, видимая
    потребителю, собирается на границе сервиса из пары
    ``(tool.tool_id(), tool.definition())``.
    """

    description: str
    input_schema: ToolInputSchema


@dataclass(frozen=True)
class ToolCall:
    """
    Запрос на вызов инструмента.

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


class ToolExecutionError(Exception):
    """Ошибка выполнения инструмента.

    Бросается из :meth:`ToolsService.execute` / :meth:`Tool.execute` вместо
    возврата флагового результата. Обработка — на стороне caller-а: agent
    ловит, обогащает ``tool_call_id``-ом (который сервис не знает) и
    превращает в feedback-сообщение для LLM.
    """

    def __init__(self, tool_id: ToolId, message: str) -> None:
        super().__init__(message)
        self.tool_id = tool_id
        self.message = message


class InvalidToolArgumentError(ToolExecutionError):
    """Аргументы tool'а не прошли валидацию по :class:`ToolInputSchema`.

    Бросается :class:`SchemaArgsValidator` при отсутствии обязательного
    параметра, нарушении типа, выходе за enum, незнакомом ключе и т.п.
    Хранит имя проблемного параметра в ``param`` — middleware превращает
    в :class:`ToolFeedbackError`, и LLM на следующей итерации видит
    конкретику и может попробовать снова.

    Имя параметра «прицеплено» через префикс в сообщении —
    унаследованный ``message`` остаётся читабельным и для логов, и для
    LLM-feedback'а без отдельного форматирования.
    """

    def __init__(self, tool_id: ToolId, param: str, reason: str) -> None:
        super().__init__(tool_id, f"параметр {param!r}: {reason}")
        self.param = param
        self.reason = reason


class InvalidSchemaInvariantError(ToolExecutionError):
    """Cross-field инвариант :class:`ToolInputSchema` нарушен.

    Отличается от :class:`InvalidToolArgumentError` тем, что проблема не
    в одном конкретном параметре, а в сочетании нескольких. Для LLM это
    даёт отдельный ``error_kind``, чтобы фидбек отличался по формулировке:
    «параметры X и Y нельзя задавать вместе» vs «параметр X — строка».
    """

    def __init__(self, tool_id: ToolId, reason: str) -> None:
        super().__init__(tool_id, f"нарушен инвариант схемы: {reason}")
        self.reason = reason


class SchemaArgsValidator(Validator[dict[str, Any]]):
    """Валидирует сырой dict аргументов против :class:`ToolInputSchema`.

    Контракт:
    - для каждого param в схеме: достаёт ключ из value (или
      :data:`MISSING`), прогоняет через ``param.validator``;
      :class:`ParamValidationError` оборачивает в
      :class:`InvalidToolArgumentError` с именем параметра и tool_id;
    - если после валидации значение не :data:`MISSING` — кладёт в
      результат; иначе пропускает (опциональный параметр без default'а);
    - незнакомые ключи отвергает с
      :class:`InvalidToolArgumentError` — strict-режим, чтобы ловить
      галлюцинации модели и опечатки.
    """

    def __init__(self, schema: ToolInputSchema, tool_id: ToolId) -> None:
        self._schema = schema
        self._tool_id = tool_id
        self._known: frozenset[str] = frozenset(p.name for p in schema.params)

    def validate(self, value: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(value.keys()) - self._known)
        if unknown:
            raise InvalidToolArgumentError(
                self._tool_id,
                unknown[0],
                f"неизвестный параметр (известные: {sorted(self._known)})",
            )

        result: dict[str, Any] = {}
        for param in self._schema.params:
            raw = value.get(param.name, MISSING)
            try:
                validated = param.validator.validate(raw)
            except ParamValidationError as e:
                raise InvalidToolArgumentError(
                    self._tool_id, param.name, str(e)
                ) from e
            if validated is not MISSING:
                result[param.name] = validated

        try:
            return self._schema.invariants.validate(result)
        except ParamValidationError as e:
            raise InvalidSchemaInvariantError(self._tool_id, str(e)) from e


class Tool(
    Executor[None, TArgs, ToolResult],
    Definition[ToolDefinition],
    Generic[TArgs],
):
    """Базовый класс tool'а.

    Шаблонный метод :meth:`args_converter` собирает pipeline:
    :class:`SchemaArgsValidator` (валидация по схеме) →
    :meth:`typed_args_converter` (маппинг провалидированного dict в
    типизированный TArgs). Перекрывать :meth:`args_converter` нельзя —
    валидация по схеме обязательна для всех tool'ов.
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
    `except ToolExecutionError` и оборачивает в `ToolFeedbackError`
    для LLM. Контракт `Converter` про `ConverterError` нарушается
    осознанно: для tool args домен-специфичная иерархия
    `ToolExecutionError` — единый канал ошибок.
    """

    def __init__(
        self,
        validator: SchemaArgsValidator,
        typed: Converter[dict[str, Any], TArgs],
    ) -> None:
        self._validator = validator
        self._typed = typed

    def convert(self, value: dict[str, Any]) -> TArgs:
        validated = self._validator.validate(value)
        return self._typed.convert(validated)


class ToolStore:
    def __init__(self) -> None:
        self._items: dict[ToolId, Tool[Any]] = {}

    def get(self, tool_id: ToolId) -> Tool[Any] | None:
        return self._items.get(tool_id)

    def add(self, tool: Tool[Any]) -> None:
        self._items[tool.tool_id()] = tool

    def tools(self) -> Iterable[Tool[Any]]:
        return iter(self._items.values())


class ToolCatalog:
    def __init__(self, tools: Iterable[Tool[Any]]) -> None:
        self._items: dict[ToolId, Tool[Any]] = {
            tool.tool_id(): tool for tool in tools
        }

    def get(self, tool_id: ToolId) -> Tool[Any] | None:
        return self._items.get(tool_id)

    def __contains__(self, tool_id: object) -> bool:
        return tool_id in self._items

    def tools(self) -> Iterable[Tool[Any]]:
        return iter(self._items.values())

    def definitions(self) -> Iterable[ToolDefinition]:
        """Описания всех инструментов — для передачи потребителю."""
        return (tool.definition() for tool in self._items.values())


class ToolIdCollisionError(Exception):
    def __init__(
        self,
        tool_id: ToolId,
        existing_source: ToolSourceId,
        new_source: ToolSourceId,
    ) -> None:
        super().__init__(
            f"tool id {tool_id.to_wire()!r} already registered "
            f"by source {existing_source.to_wire()!r}; "
            f"rejected new source {new_source.to_wire()!r}"
        )
        self.tool_id = tool_id
        self.existing_source = existing_source
        self.new_source = new_source


class ToolSource(
    PrioritySource[ToolSourceId, ToolStore],
):
    @abstractmethod
    def tools(self) -> Iterable[Tool[Any]]: ...

    def apply(self, state: ToolStore) -> ToolStore:
        for tool in self.tools():
            tool_id = tool.tool_id()
            existing = state.get(tool_id)

            if existing:
                raise ToolIdCollisionError(
                    tool_id,
                    existing.tool_source_id(),
                    tool.tool_source_id(),
                )

            state.add(tool)

        return state


class ToolFactory(
    FoldFactory[
        ToolSourceId,
        ToolStore,
        ToolCatalog,
    ],
):
    def initial(self) -> ToolStore:
        return ToolStore()

    def finalize(self, state: ToolStore) -> ToolCatalog:
        return ToolCatalog(state.tools())


class ToolsService(Executor[None, ToolCall, ToolResult]):
    """Диспетчер tool-вызовов над :class:`ToolCatalog`.

    Маршрутизация встроена по ``call.tool_id``
    ищет :class:`Tool` в ToolCatalog и вызывает ``tool.execute``.

    Ошибка :class:`ToolExecutionError`:
    - Неизвестный tool
    - ошибка парсинга аргументов
    - произвольное исключение тула
    """

    def __init__(
        self,
        factory: ToolFactory,
    ) -> None:
        self._factory = factory
        self._catalog: ToolCatalog = ToolCatalog([])

    def rebuild_catalog(self) -> None:
        """Пересобрать каталог из источников фабрики."""
        self._catalog = self._factory.build()

    def tools(self) -> Iterable[Tool[Any]]:
        """Все собранные инструменты — если нужны и id, и definition."""
        return self._catalog.tools()

    def definitions(self) -> Iterable[ToolDefinition]:
        """Описания всех собранных инструментов — для передачи потребителю."""
        return self._catalog.definitions()

    def execute(self, ctx: None, call: ToolCall) -> ToolResult:
        tool = self._catalog.get(call.tool_id)
        if tool is None:
            raise self._unknown_tool(call.tool_id)
        try:
            args = tool.args_converter().convert(call.arguments)
            return tool.execute(ctx, args)
        except ToolExecutionError:
            raise
        except Exception as e:
            raise ToolExecutionError(
                tool_id=call.tool_id,
                message=f"{type(e).__name__}: {e}",
            ) from e

    def _unknown_tool(self, tool_id: ToolId) -> ToolExecutionError:
        available = [t.tool_id().to_wire() for t in self._catalog.tools()]
        if not available:
            msg = f"tool {tool_id.to_wire()!r} not found; no tools are registered"
        else:
            msg = (
                f"tool {tool_id.to_wire()!r} not found. "
                f"available: {', '.join(repr(a) for a in available)}"
            )
        return ToolExecutionError(tool_id=tool_id, message=msg)
