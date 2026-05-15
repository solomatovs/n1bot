"""Построение pydantic-модели аргументов из функции или callable-инстанса.

Анализирует сигнатуру через `inspect.signature` и собирает
`type[BaseModel]` через `pydantic.create_model`. Параметры с типами
из `ignore_types` пропускаются (для `ToolContext`-инжекции).

Опционально парсит Google-style docstring: `Args:` блок раскладывается
на описания параметров через `Field(description=...)`, остальное идёт
в description модели.

Имя по умолчанию:
- функция        → `fn.__name__`
- callable-инстанс → `type(obj).__name__`
"""

from __future__ import annotations

import dataclasses
import inspect
import re
from collections.abc import Callable
from typing import Annotated, Any, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, create_model

__all__ = ["CallableArgsSchema", "callable_to_args_model"]


@dataclasses.dataclass(frozen=True)
class CallableArgsSchema:
    """Результат разбора функции в pydantic-модель аргументов."""

    args_model: type[BaseModel]
    name: str
    description: str
    injected: tuple[str, ...]


def callable_to_args_model(
    obj: Callable[..., Any],
    *,
    ignore_types: tuple[type, ...] = (),
    parse_docstring: bool = False,
) -> CallableArgsSchema:
    """Разобрать функцию или callable-инстанс в pydantic-модель.

    `ignore_types` — типы параметров, исключаемые из модели (например,
    `ToolContext`); их имена возвращаются в `injected`.

    `parse_docstring=True` — Google-style парсер `Args:` блока → описания
    полей. Иначе всё description пишется только в саму модель.
    """
    meta = _resolve_callable_meta(obj)

    full_doc = meta.doc
    if parse_docstring and full_doc:
        summary, arg_docs = _parse_google_docstring(full_doc)
    else:
        summary, arg_docs = full_doc, {}

    fields: dict[str, Any] = {}
    injected: list[str] = []

    for param in meta.sig.parameters.values():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            msg = (
                f"*args/**kwargs не поддерживаются "
                f"({meta.name!r}, параметр {param.name!r})"
            )
            raise TypeError(msg)

        annotation = meta.hints.get(param.name, param.annotation)
        if annotation is inspect.Parameter.empty:
            msg = f"параметр {param.name!r} без аннотации ({meta.name!r})"
            raise TypeError(msg)

        if annotation in ignore_types:
            injected.append(param.name)
            continue

        fields[param.name] = _build_field(
            annotation=annotation,
            default=param.default,
            description=arg_docs.get(param.name, ""),
        )

    args_model = create_model(
        f"{meta.name}__Args",
        __config__=ConfigDict(frozen=True, extra="forbid"),
        __doc__=summary,
        **fields,
    )
    return CallableArgsSchema(
        args_model=args_model,
        name=meta.name,
        description=summary,
        injected=tuple(injected),
    )


def _build_field(
    annotation: Any,
    default: Any,
    description: str,
) -> tuple[Any, Any]:
    """Готовый кортеж `(annotation, Field|default|...)` для `create_model`.

    Если на аннотации есть строка-метаданные `Annotated[T, "desc", ...]`,
    она перехватывается как description (если в docstring описания нет).
    Внешний `Field(...)` создаётся только если есть что-то добавить —
    иначе пустые поля могут перебить inner `Field(...)`-метаданные.
    """
    annotation, description = _peel_description(annotation, description)
    has_default = default is not inspect.Parameter.empty

    if not description:
        # Никаких override-ов сверху аннотации; default или required.
        return (annotation, default if has_default else ...)

    if has_default:
        return (annotation, Field(default=default, description=description))
    return (annotation, Field(description=description))


def _peel_description(annotation: Any, existing: str) -> tuple[Any, str]:
    """Из `Annotated[T, "desc", ...]` забрать первую строку как description.

    Если `existing` уже непуст (взято из docstring) — не перезаписывает.
    Строковая метаданная удаляется из Annotated; pydantic'овские
    метаданные (Field/BeforeValidator/...) остаются.
    """
    metadata = getattr(annotation, "__metadata__", None)
    if not metadata:
        return annotation, existing

    description = existing
    kept: list[Any] = []
    for meta in metadata:
        if isinstance(meta, str):
            if not description:
                description = meta
            continue
        kept.append(meta)

    inner = annotation.__args__[0] if hasattr(annotation, "__args__") else annotation
    if kept:
        return Annotated[(inner, *kept)], description  # type: ignore[return-value]
    return inner, description


@dataclasses.dataclass(frozen=True)
class _CallableMeta:
    sig: inspect.Signature
    hints: dict[str, Any]
    name: str
    doc: str


def _resolve_callable_meta(obj: Callable[..., Any]) -> _CallableMeta:
    if inspect.isfunction(obj) or inspect.ismethod(obj):
        target_for_hints: Any = obj
        name = obj.__name__
    elif callable(obj) and not isinstance(obj, type):
        target_for_hints = type(obj).__call__
        name = type(obj).__name__
    else:
        msg = (
            f"callable_to_args_model: ожидается функция или callable-инстанс, "
            f"получено {obj!r}"
        )
        raise TypeError(msg)

    return _CallableMeta(
        sig=inspect.signature(obj),
        hints=_safe_get_type_hints(target_for_hints),
        name=name,
        doc=inspect.getdoc(obj) or "",
    )


def _safe_get_type_hints(target: Any) -> dict[str, Any]:
    """`get_type_hints` без `return`-annotation: только параметры.

    Возвратный тип может ссылаться на локальный класс (что под
    `from __future__ import annotations` ломает `get_type_hints`).
    Для `callable_to_args_model` он не нужен — резолвим только параметры.
    """
    try:
        return get_type_hints(target, include_extras=True)
    except NameError:
        pass

    sig = inspect.signature(target)
    hints: dict[str, Any] = {}
    for param in sig.parameters.values():
        try:
            hint = get_type_hints(
                _wrap_single_param(target, param.name),
                include_extras=True,
            )
        except NameError:
            hints[param.name] = param.annotation
            continue
        if param.name in hint:
            hints[param.name] = hint[param.name]
    return hints


def _wrap_single_param(target: Any, param_name: str) -> Callable[..., None]:
    """Обёртка с одной аннотацией — изолирует резолв одного параметра.

    Если в исходной функции return-тип unresolvable, его всё равно нет тут.
    """
    annotation = target.__annotations__.get(param_name)
    if annotation is None:
        return lambda: None  # type: ignore[return-value]

    def _stub() -> None:
        pass

    _stub.__annotations__ = {param_name: annotation}
    _stub.__globals__.update(getattr(target, "__globals__", {}))
    return _stub


# Google-style docstring

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
