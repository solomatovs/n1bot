"""Базовый класс Tool, value-объекты вызова и tool-specific обёртка args-конвертации."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property
from typing import (
    Any,
    Generic,
    Protocol,
    TypeVar,
    get_args,
    get_origin,
    runtime_checkable,
)

from pydantic import BaseModel, ValidationError

from boba.patterns import Converter, Definition, Executor
from boba.schema.declaration import (
    FieldPathError,
    FieldPathMissingError,
    ObjectSchema,
)
from boba.schema.from_dataclass import schema_from_dataclass
from boba.tools.domain.args import ToolArgsBuilder
from boba.tools.domain.errors import (
    InvalidSchemaInvariantError,
    InvalidToolArgumentError,
)
from boba.tools.domain.ids import (
    ToolId,
    ToolName,
    ToolSourceId,
    compose_tool_id,
)
from boba.tools.domain.llm_schema import clean_llm_json_schema
from boba.tools.domain.result import ToolResult
from boba.tools.domain.wire import ToolWireSchemaBuilder

__all__ = [
    "JsonSchemaOverlay",
    "SchemaOverlay",
    "Tool",
    "ToolCall",
    "ToolContext",
    "ToolResult",
]

TArgs = TypeVar("TArgs")
TConfig = TypeVar("TConfig")


@runtime_checkable
class SchemaOverlay(Protocol):
    """Legacy-protocol overlay'я: умеет применить себя к `ObjectSchema`.

    Использовался до миграции на pydantic. После — оставлен только для
    tool'ов, чей TArgs ещё `@dataclass`. Pydantic-tool'ы используют
    `JsonSchemaOverlay`-протокол.
    """

    def apply(self, schema: ObjectSchema[Any]) -> ObjectSchema[Any]: ...


@runtime_checkable
class JsonSchemaOverlay(Protocol):
    """Структурный protocol overlay'я для JSON-schema dict.

    Реализуется `boba.plugin.prompt.PromptOverlay`. Domain-слой не знает
    про plugin-слой — duck-typed Protocol через метод
    `apply_to_json_schema`.
    """

    def apply_to_json_schema(
        self,
        schema: dict[str, Any],
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ToolContext:
    """Per-call контекст вызова tool'а.

    Сейчас пуст: все стабильные tool-зависимости (workspace, HTTP-клиенты,
    БД-коннекшены) привязываются на build-time через `ExtensionContext`.
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


class Tool(
    Executor[ToolContext, TArgs, ToolResult],
    Definition[dict[str, Any]],
    Generic[TArgs, TConfig],
):
    """Базовый класс tool; application-singleton.

    Соглашения по subclass'ам:
    - наследуют `Tool[XArgs, XToolConfig]` (оба type-var зафиксированы);
    - реализуют только `execute(ctx, args: TArgs) -> ToolResult`;
    - `name()`, `tool_id()`, `definition()` имеют дефолтные реализации
      на базе TArgs/TConfig и могут быть переопределены.

    `TArgs`:
    - `BaseModel`-subclass — pydantic-путь: `model_validate` + `model_json_schema`;
    - `@dataclass` — legacy-путь через `boba.schema` (поэтапная миграция).

    Соглашения по cfg:
    - конфиг хранится в `self._cfg`;
    - если у конфига есть атрибут `prompt: PromptOverlay`, он применяется
      поверх дефолтной схемы.

    Identity: tool_id = `<source>/<name>`. По умолчанию `name()` —
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

    def definition(self) -> dict[str, Any]:
        """JSON-schema параметров (для LLM): автоген + cfg.prompt overlay.

        TArgs резолвится из объявления `Tool[XArgs, XConfig]`. Pydantic-args
        → `model_json_schema()`. Legacy-dataclass → `schema_from_dataclass`
        + `ToolWireSchemaBuilder`. `cfg.prompt` (если есть) патчит итоговый
        dict через `PromptOverlay.apply_to_json_schema`.
        """
        args_type = self._try_resolve_args_type()
        if args_type is not None and issubclass(args_type, BaseModel):
            raw = args_type.model_json_schema()
        elif args_type is not None:
            raw = ToolWireSchemaBuilder(
                schema_from_dataclass(args_type),
            ).build()
        else:
            msg = (
                f"{type(self).__name__}.definition: невозможно автоматически "
                f"построить схему — TArgs не резолвится. Переопредели "
                f"definition() в подклассе."
            )
            raise TypeError(msg)

        prompt = getattr(self._cfg, "prompt", None)
        if isinstance(prompt, JsonSchemaOverlay):
            raw = prompt.apply_to_json_schema(raw)
        return clean_llm_json_schema(raw)

    @classmethod
    def _try_resolve_args_type(cls) -> type | None:
        """`TArgs` как concrete `type` или None, если резолв невозможен.

        Возвращает None, если базовый `Tool[X, Y]` параметризован не
        классом (например, `dict[str, Any]` — generic alias). Это валидный
        кейс для `DecoratedTool` / других tool'ов, которые сами строят
        adapter/definition.
        """
        for klass in cls.__mro__:
            for base in getattr(klass, "__orig_bases__", ()):
                origin = get_origin(base)
                if isinstance(origin, type) and issubclass(origin, Tool):
                    targs = get_args(base)
                    if targs and isinstance(targs[0], type):
                        return targs[0]
        return None

    @classmethod
    def _resolve_args_type(cls) -> type:
        """`TArgs` как concrete `type`; иначе TypeError."""
        result = cls._try_resolve_args_type()
        if result is None:
            msg = (
                f"{cls.__name__}: не удалось определить TArgs; "
                f"наследуй от Tool[XArgs, XConfig] с конкретным типом."
            )
            raise TypeError(msg)
        return result

    def invoke(self, ctx: ToolContext, raw: dict[str, Any]) -> ToolResult:
        """Распарсить `raw` через TArgs-схему и делегировать в `execute`."""
        args = self._parse_args(raw)
        return self.execute(ctx, args)

    def _parse_args(self, raw: dict[str, Any]) -> TArgs:
        """`raw` → typed TArgs; ошибки заворачиваются в InvalidToolArgumentError."""
        args_type = self._try_resolve_args_type()
        if args_type is not None and issubclass(args_type, BaseModel):
            return self._parse_pydantic(args_type, raw)
        return self._args_adapter.convert(raw)

    def _parse_pydantic(
        self,
        args_type: type[BaseModel],
        raw: dict[str, Any],
    ) -> Any:
        try:
            return args_type.model_validate(
                raw, context=self._validation_context(),
            )
        except ValidationError as e:
            raise _pydantic_error_to_tool_error(
                e, self.tool_id(), self.definition(), raw,
            ) from e

    def _validation_context(self) -> dict[str, Any]:
        """Context для `model_validate(raw, context=...)`.

        Default — пусто. Tool с runtime-параметрами в констрейнтах
        (например, `KbSearchTool.max_top_k`) переопределяет: возвращает
        `{"max_top_k": self._cfg.max_top_k}`, который читает
        `@field_validator(mode="after")` на Args.
        """
        return {}

    @cached_property
    def _args_adapter(self) -> _ToolArgsAdapter[TArgs]:
        """Legacy: per-tool adapter поверх `ToolArgsBuilder` (для @dataclass TArgs).

        Default: строит схему из `schema_from_dataclass(TArgs)`. Подклассы
        с предварительно построенной схемой (например, `DecoratedTool`)
        переопределяют это свойство.
        """
        args_type = self._resolve_args_type()
        schema = schema_from_dataclass(args_type)
        prompt = getattr(self._cfg, "prompt", None)
        if isinstance(prompt, SchemaOverlay):
            schema = prompt.apply(schema)
        return _ToolArgsAdapter(schema, self.tool_id())


def _pydantic_error_to_tool_error(
    err: ValidationError,
    tool_id: ToolId,
    wire_schema: dict[str, Any],
    raw: dict[str, Any],
) -> InvalidToolArgumentError | InvalidSchemaInvariantError:
    """Pydantic ValidationError → InvalidToolArgumentError/InvalidSchemaInvariantError.

    Берём первую ошибку из `err.errors()` и маппим:
    - `type == "missing"` / валидаторы на отдельных полях → `InvalidToolArgumentError`;
    - валидаторы на корне модели (`loc=()`) → `InvalidSchemaInvariantError`;
    - все остальные кейсы трактуются как per-field.
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
    props = wire_schema.get("properties", {}) if isinstance(wire_schema, dict) else {}
    expected = props.get(head) if isinstance(props, dict) else None
    received = raw.get(head)
    return InvalidToolArgumentError(
        tool_id,
        field_path,
        msg,
        expected=expected if isinstance(expected, dict) else None,
        received=received,
    )


class _ToolArgsAdapter(Converter[dict[str, Any], TArgs], Generic[TArgs]):
    """Legacy: адаптер ToolArgsBuilder для tool'ов с @dataclass TArgs.

    Сохраняется до завершения миграции всех Args на pydantic.
    """

    def __init__(self, schema: ObjectSchema[TArgs], tool_id: ToolId) -> None:
        self._schema = schema
        self._builder: ToolArgsBuilder[TArgs] = ToolArgsBuilder(schema)
        self._tool_id = tool_id
        self._known: frozenset[str] = frozenset(f.name for f in schema.fields)

    def convert(self, value: dict[str, Any]) -> TArgs:
        unknown = sorted(set(value.keys()) - self._known)
        if unknown:
            raise InvalidToolArgumentError(
                self._tool_id,
                unknown[0],
                f"неизвестный параметр (известные: {sorted(self._known)})",
            )
        try:
            return self._builder.build(value)
        except FieldPathMissingError as e:
            raise InvalidToolArgumentError(
                self._tool_id,
                e.field_name,
                self._strip_field_prefix(str(e), e.field_name),
                expected=self._field_wire_schema(e.field_name),
                received=value.get(e.field_name),
            ) from e
        except FieldPathError as e:
            if e.field_name == "<invariants>":
                raise InvalidSchemaInvariantError(self._tool_id, str(e)) from e
            raise InvalidToolArgumentError(
                self._tool_id,
                e.field_name,
                self._strip_field_prefix(str(e), e.field_name),
                expected=self._field_wire_schema(e.field_name),
                received=value.get(e.field_name),
            ) from e

    def _field_wire_schema(self, field_name: str) -> dict[str, Any] | None:
        """JSON-Schema fragment поля для подсказки LLM о ожидаемом формате."""
        wire = ToolWireSchemaBuilder(self._schema).build()
        return wire.get("properties", {}).get(field_name)

    @staticmethod
    def _strip_field_prefix(message: str, field_name: str) -> str:
        """Снимает повторяющийся 'field 'X': ' префикс из вложенной FieldPathError."""
        prefix = f"field {field_name!r}: "
        if message.startswith(prefix):
            return message[len(prefix):]
        return message
