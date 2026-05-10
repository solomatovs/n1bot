"""Декоратор @tool: функция → ToolFactory через анализ сигнатуры.

Анализирует параметры через `inspect.signature` + `get_type_hints(...,
include_extras=True)` и собирает `ObjectSchema[dict[str, Any]]` из
аннотаций. Параметр с типом `ToolContext` помечается как injected — в
схему не попадает, в `execute` прокидывается отдельно.

Возвращает `ToolFactory`. ToolId формируется при `factory.build(source_id)`,
поэтому декоратор не привязан к конкретному `ToolSource`.

Поддерживаемые типы параметров:
- скаляры: `str` / `int` / `float` / `bool`
- `Literal[...]`
- `Annotated[T, "description", *Coercer-instances]`
- `T | None` / `Optional[T]` — Nullable + auto Default(None)
- `list[T]` / `dict[str, T]` — CollectionField (T может быть dataclass)
- dataclass — NestedField (рекурсивно)

Тип возврата функции:
- `ToolResult` (TextResult/JsonResult) — пробрасывается as-is
- `str` — `TextResult(text=value)`
- `int` / `float` / `bool` — `TextResult(text=str(value))`
- `dict` — `JsonResult(payload=value)`
- `list` / `tuple` — `JsonResult(payload=list(value))`
- dataclass-инстанс — `JsonResult(payload=dataclasses.asdict(value))`
- `None` / прочее — `TypeError`
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import types
from collections.abc import Callable
from typing import (
    Any,
    ClassVar,
    Literal,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)

from boba.coercion import (
    ChainCoercer,
    Coercer,
    Default,
    IsBool,
    IsInt,
    IsNumber,
    IsString,
    NotNull,
    Nullable,
    OneOf,
    Required,
)
from boba.declaration import (
    CollectionField,
    FieldKind,
    FieldSpec,
    IndexedShape,
    KeyedShape,
    NestedField,
    ObjectItem,
    ObjectSchema,
    ScalarItem,
)
from boba.tools.domain.ids import ToolId, ToolName, ToolSourceId
from boba.tools.domain.result import JsonResult, TextResult, ToolResult
from boba.tools.domain.tool import Tool, ToolContext

__all__ = ["ToolFactory", "tool"]

_NONE_TYPE = type(None)
_DICT_TYPE_ARITY = 2


class ToolFactory:
    """
    Результат @tool: связывает декорированную функцию со схемой.

    Декоратор не знает source_id — поэтому возвращает factory. ToolId
    компонуется в `build(source_id)`. Это совпадает с текущим стилем,
    где обычные tool-классы получают `source_id` в `__init__`.
    """

    _TYPE_TO_COERCER: ClassVar[dict[type, Coercer[Any, Any]]] = {
        str: IsString(),
        int: IsInt(),
        float: IsNumber(),
        bool: IsBool(),
    }

    def __init__(
        self,
        fn: Callable[..., Any],
        name: ToolName,
        description: str,
        schema: ObjectSchema[dict[str, Any]],
        injects_ctx: bool,
    ) -> None:
        self._fn = fn
        self._name = name
        self._description = description
        self._schema = schema
        self._injects_ctx = injects_ctx

    @property
    def name(self) -> ToolName:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def schema(self) -> ObjectSchema[dict[str, Any]]:
        return self._schema

    @property
    def injects_ctx(self) -> bool:
        return self._injects_ctx

    def build(self, source_id: ToolSourceId) -> Tool[dict[str, Any]]:
        """Привязать к source_id и вернуть готовый Tool."""
        return _DecoratedTool(
            fn=self._fn,
            tool_id=ToolId.compose(source_id, self._name),
            schema=self._schema,
            injects_ctx=self._injects_ctx,
        )

    @classmethod
    def from_function(
        cls,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        parse_docstring: bool = False,
    ) -> ToolFactory:
        """Public-фабрика без декоратор-сахара. Используется и из @tool, и напрямую."""
        sig = inspect.signature(fn)
        hints = get_type_hints(fn, include_extras=True)

        full_doc = inspect.getdoc(fn) or ""
        if parse_docstring and full_doc:
            summary, arg_docs = _parse_google_docstring(full_doc)
        else:
            summary, arg_docs = full_doc, {}

        fields: list[FieldKind] = []
        injects_ctx = False

        for param in sig.parameters.values():
            if param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                msg = (
                    f"@tool: *args/**kwargs не поддерживаются "
                    f"({fn.__name__!r}, параметр {param.name!r})"
                )
                raise TypeError(msg)

            annotation = hints.get(param.name, param.annotation)
            if annotation is inspect.Parameter.empty:
                msg = f"@tool: параметр {param.name!r} без аннотации ({fn.__name__!r})"
                raise TypeError(msg)

            if annotation is ToolContext:
                if injects_ctx:
                    msg = (
                        f"@tool: параметр ToolContext должен быть один "
                        f"({fn.__name__!r}, повторно: {param.name!r})"
                    )
                    raise TypeError(msg)
                injects_ctx = True
                continue

            fields.append(
                cls._build_field(
                    fn.__name__,
                    param.name,
                    annotation,
                    param.default,
                    arg_docs.get(param.name, ""),
                ),
            )

        description_final = description if description is not None else summary
        name_final = name if name is not None else fn.__name__

        schema = ObjectSchema(
            fields=fields,
            factory=dict,
            description=description_final,
        )
        return cls(
            fn=fn,
            name=ToolName(name_final),
            description=description_final,
            schema=schema,
            injects_ctx=injects_ctx,
        )

    @classmethod
    def _build_field(
        cls,
        owner: str,
        field_name: str,
        annotation: Any,
        default: Any,
        docstring_desc: str,
    ) -> FieldKind:
        inner, is_optional, extras = _normalize(annotation)

        description, annotated_coercers = cls._parse_extras(
            owner,
            field_name,
            extras,
        )
        if not description:
            description = docstring_desc

        # ── Nested dataclass ──
        if _is_dataclass_type(inner):
            if annotated_coercers or is_optional:
                msg = (
                    f"@tool: вложенный dataclass {inner.__name__!r} для "
                    f"{field_name!r} в {owner!r}: Annotated-coercer'ы и "
                    f"Optional пока не поддерживаются"
                )
                raise TypeError(msg)
            return NestedField(
                name=field_name,
                schema=cls._schema_from_dataclass(inner),
                description=description,
            )

        # ── Коллекции ──
        origin = get_origin(inner)
        if origin is list:
            return cls._build_collection(
                owner,
                field_name,
                inner,
                IndexedShape(),
                description,
            )
        if origin is dict:
            return cls._build_collection(
                owner,
                field_name,
                inner,
                KeyedShape(),
                description,
                is_keyed=True,
            )

        # ── Скаляр ──
        return cls._build_scalar(
            owner,
            field_name,
            inner,
            is_optional,
            default,
            annotated_coercers,
            description,
        )

    @classmethod
    def _build_scalar(  # noqa: PLR0913
        cls,
        owner: str,
        field_name: str,
        inner: Any,
        is_optional: bool,
        default: Any,
        annotated_coercers: list[Coercer[Any, Any]],
        description: str,
    ) -> FieldSpec[Any]:
        coercers: list[Coercer[Any, Any]] = []
        has_default = default is not inspect.Parameter.empty

        if has_default:
            coercers.append(Default(default))
        elif is_optional:
            coercers.append(Default(None))
        else:
            coercers.append(Required())

        type_coercers = cls._type_coercers(owner, field_name, inner)

        if is_optional:
            # Nullable оборачивает type-guard + Annotated-ограничения:
            # None/MISSING → None, иначе делегирует внутрь.
            coercers.append(
                Nullable(ChainCoercer(*type_coercers, *annotated_coercers)),
            )
        else:
            coercers.extend(type_coercers)
            coercers.extend(annotated_coercers)

        return FieldSpec(
            name=field_name,
            coercer=ChainCoercer(*coercers),
            description=description,
        )

    @classmethod
    def _build_collection(  # noqa: PLR0913
        cls,
        owner: str,
        field_name: str,
        annotation: Any,
        shape: IndexedShape[Any] | KeyedShape[Any],
        description: str,
        *,
        is_keyed: bool = False,
    ) -> CollectionField[Any, Any, Any]:
        args = get_args(annotation)
        if is_keyed:
            if len(args) != _DICT_TYPE_ARITY or args[0] is not str:
                msg = (
                    f"@tool: dict для {field_name!r} в {owner!r} "
                    f"поддерживает только str-ключи (получено {args!r})"
                )
                raise TypeError(msg)
            elem_type = args[1]
        else:
            if len(args) != 1:
                msg = (
                    f"@tool: list для {field_name!r} в {owner!r} "
                    f"требует один параметр типа (получено {args!r})"
                )
                raise TypeError(msg)
            elem_type = args[0]

        reader = cls._build_item_reader(owner, field_name, elem_type)
        return CollectionField(
            name=field_name,
            reader=reader,
            shape=shape,
            description=description,
        )

    @classmethod
    def _build_item_reader(
        cls,
        owner: str,
        field_name: str,
        elem_type: Any,
    ) -> ScalarItem[Any] | ObjectItem[Any]:
        inner, is_optional, extras = _normalize(elem_type)

        if is_optional:
            msg = (
                f"@tool: Optional-элементы коллекции не поддерживаются "
                f"({owner!r}.{field_name!r})"
            )
            raise TypeError(msg)

        if _is_dataclass_type(inner):
            if extras:
                msg = (
                    f"@tool: Annotated-метаданные на dataclass-элементе "
                    f"коллекции не поддерживаются ({owner!r}.{field_name!r})"
                )
                raise TypeError(msg)
            return ObjectItem(schema=cls._schema_from_dataclass(inner))

        _, annotated_coercers = cls._parse_extras(owner, field_name, extras)

        type_coercers = cls._type_coercers(owner, field_name, inner)
        return ScalarItem(
            coercer=ChainCoercer(NotNull(), *type_coercers, *annotated_coercers),
        )

    @classmethod
    def _schema_from_dataclass(cls, dc: type) -> ObjectSchema[Any]:
        """Рекурсивно строит ObjectSchema из dataclass."""
        hints = get_type_hints(dc, include_extras=True)
        fields: list[FieldKind] = []

        for f in dataclasses.fields(dc):
            annotation = hints.get(f.name, f.type)
            default = _dataclass_field_default(f)
            fields.append(
                cls._build_field(
                    dc.__name__,
                    f.name,
                    annotation,
                    default,
                    docstring_desc="",
                ),
            )

        return ObjectSchema(
            fields=fields,
            factory=dc,
            description=inspect.getdoc(dc) or "",
        )

    @classmethod
    def _type_coercers(
        cls,
        owner: str,
        field_name: str,
        inner: Any,
    ) -> list[Coercer[Any, Any]]:
        if get_origin(inner) is Literal:
            values = get_args(inner)
            literal_types = {type(v) for v in values}
            out: list[Coercer[Any, Any]] = []
            if len(literal_types) == 1:
                guard = cls._TYPE_TO_COERCER.get(next(iter(literal_types)))
                if guard is not None:
                    out.append(guard)
            out.append(OneOf(*values))
            return out

        guard = cls._TYPE_TO_COERCER.get(inner)
        if guard is None:
            msg = (
                f"@tool: неподдерживаемый тип параметра {field_name!r} "
                f"в {owner!r}: {inner!r} "
                f"(поддерживаются: str/int/float/bool, Literal[...], "
                f"T | None, list[T], dict[str, T], dataclass)"
            )
            raise TypeError(msg)
        return [guard]

    @classmethod
    def _parse_extras(
        cls,
        owner: str,
        field_name: str,
        extras: tuple[Any, ...],
    ) -> tuple[str, list[Coercer[Any, Any]]]:
        description = ""
        annotated_coercers: list[Coercer[Any, Any]] = []
        for meta in extras:
            if isinstance(meta, str):
                if not description:
                    description = meta
            elif isinstance(meta, Coercer):
                annotated_coercers.append(meta)
            else:
                msg = (
                    f"@tool: неподдерживаемая Annotated-метаданная "
                    f"для {field_name!r} в {owner!r}: {meta!r} "
                    f"(ожидается str или Coercer)"
                )
                raise TypeError(msg)
        return description, annotated_coercers


class _DecoratedTool(Tool[dict[str, Any]]):
    """Concrete Tool, рождённый @tool. tool_id заполнен через ToolFactory.build."""

    def __init__(
        self,
        fn: Callable[..., Any],
        tool_id: ToolId,
        schema: ObjectSchema[dict[str, Any]],
        injects_ctx: bool,
    ) -> None:
        self._fn = fn
        self._tool_id_value = tool_id
        self._schema = schema
        self._injects_ctx = injects_ctx

    def tool_id(self) -> ToolId:
        return self._tool_id_value

    def definition(self) -> ObjectSchema[dict[str, Any]]:
        return self._schema

    def execute(
        self,
        ctx: ToolContext,
        args: dict[str, Any],
    ) -> ToolResult:
        raw = self._fn(ctx, **args) if self._injects_ctx else self._fn(**args)
        return _coerce_result(self._tool_id_value, raw)


_ResultRule = tuple[Callable[[Any], bool], Callable[[Any], ToolResult]]

_RESULT_COERCERS: tuple[_ResultRule, ...] = (
    (lambda v: v is None, lambda _: TextResult(text="null")),
    (lambda v: isinstance(v, ToolResult), lambda v: v),
    (lambda v: isinstance(v, str), lambda v: TextResult(text=v)),
    (lambda v: isinstance(v, (bool, int, float)), lambda v: TextResult(text=str(v))),
    (
        lambda v: dataclasses.is_dataclass(v) and not isinstance(v, type),
        lambda v: JsonResult(payload=dataclasses.asdict(v)),
    ),
    (lambda v: isinstance(v, dict), lambda v: JsonResult(payload=v)),
    (
        lambda v: isinstance(v, (list, tuple, set, frozenset)),
        lambda v: JsonResult(payload=list(v)),
    ),
)


def _coerce_result(tool_id: ToolId, value: Any) -> ToolResult:
    """Привести возврат функции к ToolResult.

    Маппинг:
        None                           → TextResult(text="null")  (как json.dumps)
        ToolResult                     → as-is
        str                            → TextResult(text=value)
        int / float / bool             → TextResult(text=str(value))
        dict                           → JsonResult(payload=value)
        list / tuple / set / frozenset → JsonResult(payload=list(value))
        dataclass-инстанс              → JsonResult(payload=dataclasses.asdict(value))
        прочее                         → TypeError
    """
    for predicate, convert in _RESULT_COERCERS:
        if predicate(value):
            return convert(value)
    msg = (
        f"@tool: функция {tool_id.to_wire()!r} вернула неподдерживаемый тип "
        f"{type(value).__name__} (ожидается ToolResult / str / int / float / "
        f"bool / list / tuple / set / dict / dataclass / None)"
    )
    raise TypeError(msg)


def _normalize(annotation: Any) -> tuple[Any, bool, tuple[Any, ...]]:
    """Снять Annotated и Optional в любом порядке.

    Возвращает: (внутренний тип, is_optional, accumulated extras).
    """
    extras: tuple[Any, ...] = ()
    is_optional = False
    while True:
        if hasattr(annotation, "__metadata__"):
            extras = extras + tuple(annotation.__metadata__)
            annotation = get_args(annotation)[0]
            continue
        origin = get_origin(annotation)
        if origin is Union or origin is types.UnionType:
            args = get_args(annotation)
            non_none = tuple(a for a in args if a is not _NONE_TYPE)
            if len(non_none) == len(args):
                # Union без None — не поддерживаем
                msg = (
                    f"@tool: Union с несколькими типами не поддерживается: "
                    f"{annotation!r}"
                )
                raise TypeError(msg)
            if len(non_none) != 1:
                msg = (
                    f"@tool: Union[..., None] с >1 не-None типом "
                    f"не поддерживается: {annotation!r}"
                )
                raise TypeError(msg)
            is_optional = True
            annotation = non_none[0]
            continue
        break
    return annotation, is_optional, extras


def _is_dataclass_type(obj: Any) -> bool:
    return isinstance(obj, type) and dataclasses.is_dataclass(obj)


def _dataclass_field_default(f: dataclasses.Field[Any]) -> Any:
    if f.default is not dataclasses.MISSING:
        return f.default
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        return f.default_factory()
    return inspect.Parameter.empty


_DOCSTRING_SECTION_RE = re.compile(
    r"^(Args|Arguments|Parameters|Returns|Raises|Yields|Examples?|Notes?):\s*$",
)
_DOCSTRING_ARG_RE = re.compile(r"^(\w+)(?:\s*\([^)]*\))?\s*:\s*(.*)$")


def _split_summary_and_args_block(lines: list[str]) -> tuple[str, int]:
    """Найти границу summary / Args-блока в строках docstring."""
    summary_lines: list[str] = []
    args_start = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped in ("Args:", "Arguments:", "Parameters:"):
            args_start = i + 1
            break
        if _DOCSTRING_SECTION_RE.match(stripped):
            break
        summary_lines.append(line)
    return "\n".join(summary_lines).rstrip(), args_start


def _parse_args_block(lines: list[str]) -> dict[str, str]:
    """Распарсить блок Args: в Google-style docstring → {name: description}."""
    arg_docs: dict[str, str] = {}
    arg_indent: int | None = None
    current_arg: str | None = None
    current_parts: list[str] = []

    def _commit() -> None:
        if current_arg is not None:
            arg_docs[current_arg] = " ".join(p.strip() for p in current_parts).strip()

    for line in lines:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        if _DOCSTRING_SECTION_RE.match(stripped) and (
            arg_indent is None or indent <= arg_indent
        ):
            break

        match = _DOCSTRING_ARG_RE.match(stripped)
        if match and (arg_indent is None or indent == arg_indent):
            _commit()
            arg_indent = indent
            current_arg = match.group(1)
            current_parts = [match.group(2)]
        else:
            current_parts.append(stripped)

    _commit()
    return arg_docs


def _parse_google_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """Google-style docstring → (summary, {arg_name: desc}).

    Summary — всё до первого секционного заголовка. Args/Arguments/Parameters —
    блок описаний параметров; формат `name: description` (с возможным `(type)`
    после имени). Многострочные описания продолжаются с большим отступом.
    """
    lines = doc.splitlines()
    summary, args_start = _split_summary_and_args_block(lines)
    if args_start < 0:
        return summary, {}
    return summary, _parse_args_block(lines[args_start:])


@overload
def tool(name_or_fn: Callable[..., Any], /) -> ToolFactory: ...

@overload
def tool(
    name_or_fn: str | None = None,
    /,
    *,
    description: str | None = None,
    parse_docstring: bool = False,
) -> Callable[[Callable[..., Any]], ToolFactory]: ...

def tool(
    name_or_fn: Callable[..., Any] | str | None = None,
    /,
    *,
    description: str | None = None,
    parse_docstring: bool = False,
) -> ToolFactory | Callable[[Callable[..., Any]], ToolFactory]:
    """Превратить функцию в `ToolFactory`.

    Формы:
        @tool                                       — bare
        @tool("custom_name")                        — переопределить имя
        @tool(description="...")                    — переопределить описание
        @tool(parse_docstring=True)                 — Google-style парсер
        @tool("custom", description="...")          — комбинация

    Маппинг сигнатуры → ObjectSchema:
        ctx: ToolContext         — injected, не попадает в схему
        path: str                — Required() + IsString()
        count: int = 50          — Default(50) + IsInt()
        path: str | None         — Default(None) + Nullable(IsString())
        Annotated[str, "путь"]   — то же + description="путь"
        Annotated[int, "...", MinValue(1)] — доп. coercer'ы из метаданных
        Literal["a", "b"]        — type-guard + OneOf("a", "b")
        list[str] / dict[str, T] — CollectionField (IndexedShape/KeyedShape)
        SomeDataclass            — NestedField (рекурсивная schema)
        list[SomeDataclass]      — CollectionField + ObjectItem
    """
    if callable(name_or_fn) and not isinstance(name_or_fn, str):
        return ToolFactory.from_function(name_or_fn)

    name_override = name_or_fn

    def decorator(fn: Callable[..., Any]) -> ToolFactory:
        return ToolFactory.from_function(
            fn,
            name=name_override,
            description=description,
            parse_docstring=parse_docstring,
        )

    return decorator
