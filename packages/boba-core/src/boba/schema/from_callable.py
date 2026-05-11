"""
Построение ObjectSchema из функции или callable-объекта

Анализирует сигнатуру функции через `inspect.signature`
и собирает `ObjectSchema[dict[str, Any]]`

Имя по умолчанию:
- функция        → `fn.__name__`
- callable-инстанс → `type(obj).__name__`

Параметры с типами из `ignore_types` пропускаются и попадают в
`CallableSchema.injected` — это даёт пользователю (например, tools-фабрике)
точку, где можно реализовать инъекцию контекста (`ToolContext`), не
зашивая знание про неё в boba-core.

Опционально парсит Google-style docstring: `Args:` блок раскладывается на
описания параметров, остальное идёт в `description` схемы.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
from collections.abc import Callable
from typing import Any, get_type_hints

from boba.declaration import FieldKind, ObjectSchema
from boba.schema.field import build_field_from_annotation

__all__ = ["CallableSchema", "schema_from_callable"]


@dataclasses.dataclass(frozen=True)
class CallableSchema:
    """
    Результат разбора функции/callable-инстанса
    """

    schema: ObjectSchema[dict[str, Any]]
    name: str
    description: str
    injected: tuple[str, ...]


def schema_from_callable(
    obj: Callable[..., Any],
    *,
    ignore_types: tuple[type, ...] = (),
    parse_docstring: bool = False,
) -> CallableSchema:
    """
    Разобрать функцию или callable-инстанс в `CallableSchema`.
    """
    meta = _resolve_callable_meta(obj)

    full_doc = meta.doc
    if parse_docstring and full_doc:
        summary, arg_docs = _parse_google_docstring(full_doc)
    else:
        summary, arg_docs = full_doc, {}

    fields: list[FieldKind] = []
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

        fields.append(
            build_field_from_annotation(
                owner=meta.name,
                field_name=param.name,
                annotation=annotation,
                default=param.default,
                docstring_desc=arg_docs.get(param.name, ""),
            ),
        )

    schema: ObjectSchema[dict[str, Any]] = ObjectSchema(
        fields=fields,
        factory=dict,
        description=summary,
    )
    return CallableSchema(
        schema=schema,
        name=meta.name,
        description=summary,
        injected=tuple(injected),
    )


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
            f"schema_from_callable: ожидается функция или callable-инстанс, "
            f"получено {obj!r}"
        )
        raise TypeError(msg)

    return _CallableMeta(
        sig=inspect.signature(obj),
        hints=get_type_hints(target_for_hints, include_extras=True),
        name=name,
        doc=inspect.getdoc(obj) or "",
    )


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
