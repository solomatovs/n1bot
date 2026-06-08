"""LLM-friendly GenerateJsonSchema для pydantic Args-моделей.

BaseModel.model_json_schema() без настройки выдаёт draft-2020-12 со
всем содержимым: $defs + $ref-ссылки на вложенные модели и title
на каждом property/sub-schema. Для LLM-tool это шум: лишние байты,
лишний уровень indirection, конфликт с strict-режимом ряда провайдеров.

LLMSchemaGenerator — sabklass, который сразу эмитит «плоскую»
LLM-схему. Передаётся в стандартный pydantic-API:

    schema = MyArgs.model_json_schema(schema_generator=LLMSchemaGenerator)

На выходе:

  * title снят на корне и в каждом property/sub-schema
    (override field_title_should_be_set + пост-проход);
  * $defs/definitions инлайнятся в местах $ref и удаляются с корня;
  * остальное (type, description, properties, required, default,
    minimum, maximum, minLength, maxLength, pattern, enum,
    anyOf, oneOf, allOf, items, additionalProperties, format,
    const, examples, …) сохраняется.

Возвращаемый dict — свежий; исходные схемы pydantic не мутируются.
"""

from __future__ import annotations

from typing import Any

from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode
from pydantic_core import CoreSchema

__all__ = ["LLMSchemaGenerator"]


_DEFS_KEYS = ("$defs", "definitions")
_DROP_KEYS = ("title",)


class LLMSchemaGenerator(GenerateJsonSchema):
    """GenerateJsonSchema-sabklass, выдающий плоскую LLM-схему."""

    def field_title_should_be_set(self, _schema: CoreSchema) -> bool:
        """Подавить title на field-level — у LLM-tool оно бесполезно."""
        return False

    def generate(
        self,
        schema: CoreSchema,
        mode: JsonSchemaMode = "validation",
    ) -> dict[str, Any]:
        """Сгенерировать схему и сразу привести к LLM-минимальной форме."""
        raw = super().generate(schema, mode)
        return _flatten(raw)


def _flatten(schema: dict[str, Any]) -> dict[str, Any]:
    """Инлайн $defs/$ref, дроп title и пустых $defs на корне."""
    defs = _collect_defs(schema)
    out = _walk(schema, defs)
    for key in _DROP_KEYS:
        out.pop(key, None)
    for key in _DEFS_KEYS:
        out.pop(key, None)
    return out


def _collect_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Собрать $defs/definitions в плоскую таблицу для inline-резолва."""
    defs: dict[str, Any] = {}
    for key in _DEFS_KEYS:
        bucket = schema.get(key)
        if isinstance(bucket, dict):
            defs.update(bucket)
    return defs


def _walk(node: Any, defs: dict[str, Any]) -> Any:
    """Рекурсивно: резолв $ref, удаление title, копия (без мутаций)."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            resolved = _resolve_ref(ref, defs)
            if resolved is not None:
                return _walk(resolved, defs)
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k in _DROP_KEYS or k in _DEFS_KEYS:
                continue
            out[k] = _walk(v, defs)
        return out
    if isinstance(node, list):
        return [_walk(item, defs) for item in node]
    return node


def _resolve_ref(ref: str, defs: dict[str, Any]) -> dict[str, Any] | None:
    """Разрешить локальную $ref через таблицу defs. Внешние — игнор."""
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
