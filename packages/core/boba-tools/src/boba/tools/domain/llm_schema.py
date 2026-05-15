"""LLM-friendly post-processing для pydantic-эмитированного JSON-schema.

`BaseModel.model_json_schema()` пишет богатую draft-2020-12 схему с
полями, которые для LLM-tools обычно бесполезны или вредят (лишний
размер, неверный strict-режим). Этот хелпер «причёсывает» dict до
минимального набора, ожидаемого `ToolSchema.parameters_schema`:

  * `title` — удалён на корне и в каждом property/sub-schema;
  * `$defs`/`definitions` — инлайнятся (рекурсивно) и удаляются
    с корня; ссылки `$ref` разрешаются;
  * остальные ключи (`type`, `description`, `properties`, `required`,
    `default`, `minimum`, `maximum`, `minLength`, `maxLength`,
    `pattern`, `enum`, `anyOf`, `oneOf`, `allOf`, `items`,
    `additionalProperties`, `format`, `const`, `examples`, ...) —
    оставлены без изменений.

Возврат — новый dict, исходный не мутируется.
"""

from __future__ import annotations

from typing import Any

__all__ = ["clean_llm_json_schema"]


_DEFS_KEYS = ("$defs", "definitions")
_DROP_AT_ROOT = ("title",)
_DROP_NESTED = ("title",)


def clean_llm_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Привести pydantic-эмитированный JSON-schema к LLM-минимальной форме."""
    defs = _collect_defs(schema)
    cleaned = _walk(schema, defs)
    for key in _DROP_AT_ROOT:
        cleaned.pop(key, None)
    for key in _DEFS_KEYS:
        cleaned.pop(key, None)
    return cleaned


def _collect_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Собрать все `$defs`/`definitions` для inline-резолва `$ref`."""
    defs: dict[str, Any] = {}
    for key in _DEFS_KEYS:
        bucket = schema.get(key)
        if isinstance(bucket, dict):
            defs.update(bucket)
    return defs


def _walk(node: Any, defs: dict[str, Any]) -> Any:
    """Рекурсивный обход: резолв `$ref`, удаление `title`, копия."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            resolved = _resolve_ref(ref, defs)
            if resolved is not None:
                return _walk(resolved, defs)
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in _DROP_NESTED or k in _DEFS_KEYS:
                continue
            out[k] = _walk(v, defs)
        return out
    if isinstance(node, list):
        return [_walk(item, defs) for item in node]
    return node


def _resolve_ref(ref: str, defs: dict[str, Any]) -> dict[str, Any] | None:
    """Разрешить локальную `$ref` через таблицу `defs`. Внешние — игнор."""
    prefix_defs = "#/$defs/"
    prefix_definitions = "#/definitions/"
    if ref.startswith(prefix_defs):
        name = ref[len(prefix_defs):]
    elif ref.startswith(prefix_definitions):
        name = ref[len(prefix_definitions):]
    else:
        return None
    target = defs.get(name)
    return target if isinstance(target, dict) else None
