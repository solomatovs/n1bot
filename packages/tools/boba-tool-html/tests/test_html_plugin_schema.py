"""Snapshot-тест: schema_from_dataclass(HtmlPluginConfig) ≡ ожидаемая структура.

Проверяет, что после миграции на автоген ObjectSchema плагина сохраняет:
  * factory, description, имена полей;
  * каждое поле — NestedField со схемой PromptOverlay;
  * структура PromptOverlay-схемы (description + fields keyed-collection).
"""

from __future__ import annotations

from boba.plugin.prompt import PromptOverlay
from boba.schema import schema_from_dataclass
from boba.schema.declaration import (
    CollectionField,
    FieldSpec,
    KeyedShape,
    NestedField,
)
from boba.tool.html import HtmlPluginConfig

_SCHEMA = schema_from_dataclass(HtmlPluginConfig)


def test_factory_is_dto():
    assert _SCHEMA.factory is HtmlPluginConfig


def test_schema_description_from_dto_docstring():
    assert _SCHEMA.description.startswith("HTML multi-tool plugin")


def test_top_level_field_names():
    assert {f.name for f in _SCHEMA.fields} == {
        "html_outline",
        "html_section",
        "tools",
    }


def test_prompt_overlay_fields_are_nested():
    for name in ("html_outline", "html_section"):
        f = next(x for x in _SCHEMA.fields if x.name == name)
        assert isinstance(f, NestedField)
        assert f.schema.factory is PromptOverlay


def test_prompt_overlay_nested_has_description_and_fields():
    nested = next(
        f for f in _SCHEMA.fields if f.name == "html_outline"
    )
    assert isinstance(nested, NestedField)
    sub_field_names = {ff.name for ff in nested.schema.fields}
    assert sub_field_names == {"description", "fields"}

    desc_f = next(ff for ff in nested.schema.fields if ff.name == "description")
    fields_f = next(ff for ff in nested.schema.fields if ff.name == "fields")
    assert isinstance(desc_f, FieldSpec)
    assert isinstance(fields_f, CollectionField)
    assert isinstance(fields_f.shape, KeyedShape)
