"""Базовый класс Tool, value-объекты вызова и pydantic-валидация аргументов."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import (
    Any,
    Generic,
    TypeVar,
    get_args,
    get_origin,
)

from pydantic import BaseModel, ValidationError

from boba.patterns import Definition
from boba.tools.domain.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
)
from boba.tools.domain.events import ToolEvent
from boba.tools.domain.ids import (
    ToolId,
    ToolName,
    ToolSourceId,
    compose_tool_id,
)
from boba.tools.domain.llm_schema import LLMSchemaGenerator
from boba.tools.domain.result import ToolResult

__all__ = [
    "Tool",
    "ToolCall",
    "ToolContext",
    "ToolResult",
    "ToolSchema",
]

TArgs = TypeVar("TArgs", bound=BaseModel)
TConfig = TypeVar("TConfig")


@dataclass(frozen=True)
class ToolContext:
    """Per-call контекст вызова tool'а.

    Сейчас пуст: все стабильные tool-зависимости (workspace, HTTP-клиенты,
    БД-коннекшены) привязываются на build-time через DI-Container агента.
    Тип сохранён как seam для будущих **реально per-call** концепций.

    Чтобы попасть сюда, концепция должна (1) меняться от вызова к вызову
    в рамках одного Tool-инстанса и (2) быть нужной достаточно широко,
    чтобы оправдать общий параметр в сигнатуре `execute`. Если редкая —
    лучше инжектить provider в конструктор конкретного tool'а и читать
    через ambient state (contextvar), чем расширять этот dataclass.

    Кандидаты, которые сюда могут лечь:

    - **Cancellation token / deadline.** Юзер прервал агентский луп,
      сработал общий timeout — нужно обрубить I/O внутри tool'а.
      По природе per-call: у каждой итерации свой дедлайн.
    - **Request id / trace id / correlation id.** Логирование, OTel-span'ы.
      Каждая итерация лупа имеет свежий id; tool пишет логи под
      правильным correlation.
    - **Identity вызывающего.** Multi-tenant ACL внутри tool'а,
      user-scoped квоты. Меняется per-call в шаренных сессиях.
    - **Progress sink.** Long-running tool эмитит события прогресса
      наружу; UI-bridge привязан к конкретному соединению.
    - **History view.** Tool, которому надо посмотреть в прошлые
      сообщения текущего диалога.
    - **Делегированный LLM-source.** Tool, который сам зовёт LLM
      (summarize/extract), хочет те же model/sampling, что у родителя
    """


@dataclass(frozen=True)
class ToolCall:
    """Запрос на вызов инструмента (qualified wire-id `<source>/<name>`)."""

    tool_id: ToolId
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolSchema:
    """Декларация tool'а: имя, описание, JSON-schema параметров.

    Живёт в tools-домене как контракт, **исходящий** из tools наружу к
    LLM-слою. Симметрично к `ToolResult`, который тоже доменная сущность
    tools, используемая LLM-протоколом (`ToolResultMessage.result`).
    """

    name: str
    description: str
    parameters_schema: Mapping[str, Any]


class Tool(
    Definition[ToolSchema],
    Generic[TArgs, TConfig],
):
    """Базовый класс tool; application-singleton.

    **Контракт исключительно streaming.** Subclass реализует один метод —
    `stream(ctx, args: TArgs) -> Iterator[ToolEvent]`. Все вызовы tool'а
    идут через него. По конвенции последний yield в потоке — `ToolStreamCompleted`
    с финальным результатом; всё, что до — `ToolProgressReported`
    (индикативный прогресс). Tool, которому нечего показывать в процессе
    — yield-ит ровно одно `ToolStreamCompleted`.

    Соглашения по subclass'ам:
    - наследуют `Tool[XArgs, XToolConfig]`, где `XArgs` — pydantic
      `BaseModel`-subclass с описанием аргументов tool'а;
    - реализуют `stream(ctx, args: XArgs) -> Iterator[ToolEvent]`;
    - `name()`, `tool_id()`, `definition()` имеют дефолтные реализации
      на базе TArgs/TConfig и могут быть переопределены.

    Identity: tool_id = `<source>__<name>`. По умолчанию `name()` —
    snake_case имени класса без суффикса `Tool` (CatTool → "cat").
    """

    _cfg: TConfig

    def __init__(
        self,
        cfg: TConfig,
        ctx: Any,
        source_id: ToolSourceId,
    ) -> None:
        self._cfg = cfg
        self._ctx = ctx
        self._source_id = source_id

    @cached_property
    def _tool_id(self) -> ToolId:
        return compose_tool_id(self._source_id, self.name())

    def tool_id(self) -> ToolId:
        return self._tool_id

    def name(self) -> ToolName:
        """Локальное имя tool'а; default — snake_case класса без суффикса 'Tool'."""
        cls_name = type(self).__name__
        if cls_name.endswith("Tool"):
            cls_name = cls_name[:-4]
        snake = re.sub(r"(?<!^)(?=[A-Z])", "_", cls_name).lower()
        return ToolName(snake)

    def source_id(self) -> ToolSourceId:
        return self._source_id

    def definition(self) -> ToolSchema:
        """`ToolSchema` инструмента: name + description + parameters_schema.

        args_model берётся из `_args_model_class()` (instance-hook; default
        резолвит TArgs из `Tool[XArgs, XConfig]` через `__orig_bases__`).
        Схема эмитится `LLMSchemaGenerator`-ом сразу плоской (без `$defs`/
        `$ref`/`title`). `description` переезжает в одноимённое поле
        `ToolSchema`, остальное идёт в `parameters_schema`.
        """
        schema = self._args_model_class().model_json_schema(
            schema_generator=LLMSchemaGenerator,
        )
        description = str(schema.pop("description", ""))
        return ToolSchema(
            name=self.tool_id(),
            description=description,
            parameters_schema=schema,
        )

    def _args_model_class(self) -> type[BaseModel]:
        """Instance-hook: какая pydantic-модель валидирует аргументы.

        Default — резолв через generic-параметр `Tool[XArgs, XConfig]`.
        Сабклассы, у которых args_model рождается не из generic (например,
        `DecoratedTool` создаёт модель через `create_model`), переопределяют.
        """
        return self._resolve_args_model()

    @classmethod
    def _resolve_args_model(cls) -> type[BaseModel]:
        """Извлечь TArgs из `Tool[XArgs, XConfig]`; иначе TypeError."""
        for klass in cls.__mro__:
            for base in getattr(klass, "__orig_bases__", ()):
                origin = get_origin(base)
                if isinstance(origin, type) and issubclass(origin, Tool):
                    targs = get_args(base)
                    if (
                        targs
                        and isinstance(targs[0], type)
                        and issubclass(targs[0], BaseModel)
                    ):
                        return targs[0]
        msg = (
            f"{cls.__name__}: не удалось определить TArgs; "
            f"наследуй от Tool[XArgs, XConfig] где XArgs — BaseModel."
        )
        raise TypeError(msg)

    def invoke_stream(
        self, ctx: ToolContext, raw: dict[str, Any],
    ) -> Iterator[ToolEvent]:
        """Public entry-point: парсит `raw` через TArgs-модель и зовёт `stream`.

        `ToolExecutor.stream(...)` зовёт именно этот метод — он отвечает за
        валидацию аргументов и пробрасывает поток `ToolEvent` от subclass'а.
        """
        args = self._parse_args(raw)
        yield from self.stream(ctx, args)

    def stream(
        self, ctx: ToolContext, args: TArgs,
    ) -> Iterator[ToolEvent]:
        """Subclass-hook: yield-ит `ToolEvent`'ы выполнения tool'а.

        Идиоматический контракт: реализация yield-ит **последним** событием
        `ToolStreamCompleted(result=...)` — это и есть результат tool'а.
        До этого может yield-ить любое число
        `ToolProgressReported` для индикации прогресса в UI.

        Базовый класс не предоставляет дефолт — каждый subclass решает сам,
        как выглядит его stream. Адаптер `DishkaTool` реализует обе формы
        (generator-функция и обычная) поверх этого hook'а.
        """
        raise NotImplementedError

    def _parse_args(self, raw: dict[str, Any]) -> TArgs:
        """`raw: dict` → typed TArgs через `model_validate`.

        Ошибки pydantic'а заворачиваются в `InvalidToolArgumentError` /
        `InvalidSchemaInvariantError` с подсказкой `expected: JSON-schema`
        и preview `received` для LLM.
        """
        args_model = self._args_model_class()
        try:
            return args_model.model_validate(  # type: ignore[return-value]
                raw,
                context=self._validation_context(),
            )
        except ValidationError as e:
            raise _pydantic_error_to_tool_error(
                e,
                self.tool_id(),
                self.definition(),
                raw,
            ) from e

    def _validation_context(self) -> dict[str, Any]:
        """Context для `model_validate(raw, context=...)`.

        Default — пусто. Tool с runtime-параметрами в констрейнтах
        (например, `KbSearchTool.max_top_k`) переопределяет: возвращает
        `{"max_top_k": self._cfg.max_top_k}`, который читает
        `@field_validator(mode="after")` на Args.
        """
        return {}


def _pydantic_error_to_tool_error(
    err: ValidationError,
    tool_id: ToolId,
    wire_schema: ToolSchema,
    raw: dict[str, Any],
) -> InvalidToolArgumentError | InvalidSchemaInvariantError:
    """Pydantic `ValidationError` → `InvalidToolArgument` / `InvalidSchemaInvariant`.

    Берётся первая ошибка из `err.errors()` и маппится:
    - `loc == ()` (root-level validator) → `InvalidSchemaInvariantError`;
    - `loc == (str_field, ...)` → `InvalidToolArgumentError` с
      `expected = parameters_schema["properties"][field]` и
      `received = raw[field]`;
    - кейсы со странным `loc` (не строка) → `InvalidSchemaInvariantError`.
    """
    errors = err.errors()
    if not errors:  # pragma: no cover — pydantic всегда возвращает >=1
        return InvalidSchemaInvariantError(tool_id, str(err))

    first = errors[0]
    loc: tuple[Any, ...] = tuple(first.get("loc", ()))
    msg: str = str(first.get("msg", ""))
    if not loc:
        return InvalidSchemaInvariantError(tool_id, msg)

    head = loc[0]
    if not isinstance(head, str):
        return InvalidSchemaInvariantError(tool_id, msg)

    field_path = ".".join(str(p) for p in loc)
    props = wire_schema.parameters_schema.get("properties", {})
    expected = props.get(head) if isinstance(props, dict) else None
    received = raw.get(head)
    return InvalidToolArgumentError(
        tool_id,
        field_path,
        msg,
        expected=expected if isinstance(expected, dict) else None,
        received=received,
    )
