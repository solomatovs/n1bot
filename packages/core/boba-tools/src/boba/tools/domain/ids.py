"""Идентификаторы tool-домена.

Имя tool'а живёт в **двух пространствах**:

- `ToolName` — локальное имя в пределах своего `ToolSource` (например,
  `outline` внутри `plugin_html`). Уникальность гарантируется самим
  source'ом — глобальной коллизии нет.
- `ToolId` — wire-формат, по которому tool вызывается LLM-ом:
  `<source_id>__<name>`. Единственная точка композиции/парсинга —
  `compose_tool_id`/`parse_tool_id`.

Wire-имя ограничено `^[A-Za-z0-9_-]{1,64}$` — это пересечение OpenAI
function-name spec и того, что без сюрпризов проходит через
LiteLLM/Ollama. Разделитель — двойное подчёркивание `__`; чтобы
парсинг был однозначным, ни `source_id`, ни `name` не должны
содержать `__` сами по себе.
"""

from __future__ import annotations

import re
from typing import NewType

__all__ = [
    "ToolId",
    "ToolName",
    "ToolSourceId",
    "compose_tool_id",
    "parse_tool_id",
    "sanitize_source_id",
]

_SEPARATOR = "__"
_MAX_TOOL_ID_LENGTH = 64
_COMPONENT_CHARS = r"A-Za-z0-9_-"
_COMPONENT_RE = re.compile(rf"^[A-Za-z0-9][{_COMPONENT_CHARS}]*$")
_NON_COMPONENT_RE = re.compile(rf"[^{_COMPONENT_CHARS}]")


ToolName = NewType("ToolName", str)
"""Локальное имя инструмента внутри своего source'а."""


ToolSourceId = NewType("ToolSourceId", str)
"""Идентификатор источника инструментов (`plugin_html`, `mcp_github`, ...)."""


ToolId = NewType("ToolId", str)
"""Wire-формат: `<source_id>__<tool_name>`. Композируется из пары."""


def _validate_component(value: str, *, kind: str) -> None:
    if not value:
        msg = f"{kind} must be non-empty"
        raise ValueError(msg)
    if not _COMPONENT_RE.match(value):
        msg = (
            f"invalid {kind} {value!r}: must match [A-Za-z0-9][A-Za-z0-9_-]* "
            f"(letters/digits/_/-, leading char alphanumeric)"
        )
        raise ValueError(msg)
    if _SEPARATOR in value:
        msg = (
            f"invalid {kind} {value!r}: must not contain separator "
            f"{_SEPARATOR!r} — это сломает round-trip parse"
        )
        raise ValueError(msg)


def compose_tool_id(source_id: ToolSourceId, name: ToolName) -> ToolId:
    """Скомпоновать qualified `<source>__<name>` ToolId.

    Валидирует обе компоненты на `[A-Za-z0-9][A-Za-z0-9_-]*` и
    отсутствие `__`, а также общую длину ≤64 символа — иначе LLM
    провайдеры (OpenAI/Ollama через LiteLLM) ругаются.
    """
    _validate_component(source_id, kind="source_id")
    _validate_component(name, kind="tool name")
    tool_id = f"{source_id}{_SEPARATOR}{name}"
    if len(tool_id) > _MAX_TOOL_ID_LENGTH:
        msg = (
            f"composed tool id {tool_id!r} is {len(tool_id)} chars, "
            f"max {_MAX_TOOL_ID_LENGTH} (OpenAI function-name limit)"
        )
        raise ValueError(msg)
    return ToolId(tool_id)


def sanitize_source_id(origin: str) -> ToolSourceId:
    """Привести произвольный `origin` (имя модуля/плагина) к валидному ToolSourceId.

    Заменяет недопустимые символы на `_`, схлопывает разделитель `__` (он
    зарезервирован под compose/parse) и обрезает ведущие/хвостовые `_`/`-`,
    чтобы id начинался и заканчивался alnum'ом — иначе соседство хвостового
    `_` с разделителем даёт неоднозначный `___` и ломает round-trip parse.
    Пустой результат заменяется на `plugin`. Результат всегда проходит
    `compose_tool_id` и round-trip'ится через `parse_tool_id`.
    """
    sanitized = _NON_COMPONENT_RE.sub("_", origin)
    while _SEPARATOR in sanitized:
        sanitized = sanitized.replace(_SEPARATOR, "_")
    sanitized = sanitized.strip("_-")
    return ToolSourceId(sanitized or "plugin")


def parse_tool_id(tool_id: ToolId) -> tuple[ToolSourceId, ToolName]:
    """
    Распарсить `ToolId` обратно в `(ToolSourceId, ToolName)`.
    """
    source_part, sep, name_part = tool_id.partition(_SEPARATOR)
    if not sep or not name_part:
        msg = (
            f"invalid qualified tool id {tool_id!r}: expected "
            f"'<source>{_SEPARATOR}<name>'"
        )
        raise ValueError(msg)

    return ToolSourceId(source_part), ToolName(name_part)
