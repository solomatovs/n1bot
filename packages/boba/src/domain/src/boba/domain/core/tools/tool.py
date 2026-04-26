"""Базовый класс Tool, value-объекты вызова и tool-specific
оркестратор валидации аргументов.

Описание tool'а — это ObjectSchema
напрямую (тот же примитив, что описывает config-секцию). Tool возвращает
его через definition; description живёт на самой схеме.

SchemaArgsValidator тут осознанно — он плотно связан с
Tool (используется в args_converter) и не имеет
смысла за пределами tool-границы (использует tool-domain ошибки
InvalidToolArgumentError / InvalidSchemaInvariantError).
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from boba.domain.core.declaration import ObjectSchema
from boba.domain.core.patterns import (
    Converter,
    ConverterInputError,
    Definition,
    Executor,
)
from boba.domain.core.tools.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
)
from boba.domain.core.tools.ids import ToolId, ToolSourceId
from boba.domain.core.validators import MISSING
from boba.domain.core.workspace import ProjectWorkspaceShell

TArgs = TypeVar("TArgs")


@dataclass(frozen=True)
class ToolContext:
    """Per-request контекст исполнения tool'а.

    Tool-классы — application-singletons (создаются один раз при
    регистрации плагина и кэшируются в ToolsService). Всё, что
    зависит от конкретной user-сессии — проходит через ToolContext,
    который middleware строит на каждый запрос и пробрасывает в
    execute.

    Сейчас контракт минимальный — только project_workspace. Если
    появится tool, которому нужны другие per-request зависимости (LLM-
    клиент, секреты, agent-config), они добавятся сюда; контракт
    расширяется по мере появления реальных потребителей.
    """

    project_workspace: ProjectWorkspaceShell


@dataclass(frozen=True)
class ToolCall:
    """Запрос на вызов инструмента.

    tool_id — к какому инструменту;
    arguments — сырой dict, каким его сформировал caller (в случае
    LLM это то, что модель сериализовала в JSON).
    """

    tool_id: ToolId
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """Результат успешного выполнения инструмента.

    Ошибки не представляются отдельным флагом — они бросаются как
    ToolExecutionError и обрабатываются caller-ом (в agent-слое
    middleware превращает их в tool-сообщение для LLM + наблюдательное
    событие).
    """

    content: str


class Tool(
    Executor[ToolContext, TArgs, ToolResult],
    Definition[ObjectSchema[dict[str, Any]]],
    Generic[TArgs],
):
    """Базовый класс tool'а.

    definition возвращает ObjectSchema[dict] —
    тот же примитив, что описывает config-секцию. Текстовое описание
    действия tool'а живёт на schema.description; набор параметров
    — в schema.fields; cross-field инварианты — в
    schema.invariants; factory=dict (tool возвращает сырой
    dict, типизирует typed_args_converter).

    Шаблонный метод args_converter собирает pipeline:
    SchemaArgsValidator (валидация по схеме) →
    typed_args_converter (маппинг провалидированного dict в
    типизированный TArgs). Перекрывать args_converter нельзя —
    валидация по схеме обязательна для всех tool'ов.

    execute(ctx: ToolContext, args: TArgs) принимает per-request
    контекст с project_workspace и любым DI, зависящим от сессии.
    Сами Tool-инстансы — application-singletons: создаются один раз при
    регистрации плагина и переиспользуются между запросами; никакого
    per-session состояния в self хранить нельзя.
    """

    @abstractmethod
    def tool_id(self) -> ToolId: ...

    @abstractmethod
    def tool_source_id(self) -> ToolSourceId: ...

    @abstractmethod
    def typed_args_converter(self) -> Converter[dict[str, Any], TArgs]:
        """Маппер провалидированного dict в типизированный TArgs.

        На вход приходит dict, уже прошедший
        SchemaArgsValidator: только известные ключи, типы
        соответствуют схеме, default'ы применены. Реализация просто
        собирает dataclass из готовых полей — без проверок и
        приведения типов.
        """
        ...

    def args_converter(self) -> Converter[dict[str, Any], TArgs]:
        """Pipeline: валидация по схеме → маппинг в TArgs.

        Метод финален de facto — переопределять не нужно: логика
        полностью выводится из definition и
        typed_args_converter.
        """
        return _ToolArgsPipeline(
            SchemaArgsValidator(self.definition(), self.tool_id()),
            self.typed_args_converter(),
        )


class _ToolArgsPipeline(Converter[dict[str, Any], TArgs], Generic[TArgs]):
    """Pipeline валидации + маппинга для args_converter.

    InvalidToolArgumentError (`ToolExecutionError`-потомок)
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


class SchemaArgsValidator(Converter[dict[str, Any], dict[str, Any]]):
    """Валидирует сырой dict аргументов против ObjectSchema.

    Контракт:
    - проверяет, что все ключи входного dict известны схеме; иначе
      InvalidToolArgumentError;
    - для каждого param в схеме: достаёт значение (или MISSING),
      прогоняет через param.converter; ConverterInputError
      оборачивает в InvalidToolArgumentError с именем
      параметра и tool_id;
    - значения, оставшиеся MISSING после конвертации, в результат
      не попадают (опциональный параметр без default'а);
    - в финале прогоняет schema.invariants над собранным dict'ом;
      ConverterInputError оттуда оборачивает в
      InvalidSchemaInvariantError.

    Tool-specific (использует InvalidToolArgumentError /
    InvalidSchemaInvariantError и ToolId) — поэтому
    лежит в tools-слое, а не в общем validate_object. Логика
    обхода полей дублируется минимально (имя поля / признак invariants
    нужны для специфичных ошибок).
    """

    def __init__(
        self,
        schema: ObjectSchema[dict[str, Any]],
        tool_id: ToolId,
    ) -> None:
        self._schema = schema
        self._tool_id = tool_id
        self._known: frozenset[str] = frozenset(p.name for p in schema.fields)

    def convert(self, value: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(value.keys()) - self._known)
        if unknown:
            raise InvalidToolArgumentError(
                self._tool_id,
                unknown[0],
                f"неизвестный параметр (известные: {sorted(self._known)})",
            )

        # Симметрично validate_object, но с tool-специфичными
        # обёртками ошибок (tool_id, имя параметра / признак invariants).
        result: dict[str, Any] = {}
        for param in self._schema.fields:
            raw = value.get(param.name, MISSING)
            try:
                validated = param.converter.convert(raw)
            except ConverterInputError as e:
                raise InvalidToolArgumentError(
                    self._tool_id, param.name, str(e)
                ) from e
            if validated is not MISSING:
                result[param.name] = validated

        try:
            final = self._schema.invariants.convert(result)
        except ConverterInputError as e:
            raise InvalidSchemaInvariantError(self._tool_id, str(e)) from e
        return self._schema.factory(**final)
