"""Snapshot-тест: schema_from_dataclass(FilesPluginConfig) и CatToolConfig-nested."""

from __future__ import annotations

from typing import Any

from boba.plugin.prompt import PromptOverlay
from boba.schema import schema_from_dataclass
from boba.schema.coercion import MISSING
from boba.schema.declaration import FieldSpec, NestedField, ObjectSchema
from boba.schema.value import StringValue
from boba.tool.files import FilesPluginConfig
from boba.tool.files.cat import CatToolConfig

_SCHEMA = schema_from_dataclass(FilesPluginConfig)


_PROMPT_NAMES = {
    "append", "cd", "cp", "edit", "grep", "ls", "mkdir",
    "mv", "pwd", "rm", "stat", "touch", "tree", "write",
}


def _cat_schema() -> ObjectSchema[Any]:
    cat = next(x for x in _SCHEMA.fields if x.name == "cat")
    assert isinstance(cat, NestedField)
    return cat.schema


# Структура FilesPluginConfig

def test_factory_is_dto():
    assert _SCHEMA.factory is FilesPluginConfig


def test_schema_description_from_dto_docstring():
    assert _SCHEMA.description.startswith("Builtin file-system tools")


def test_top_level_field_names():
    assert {f.name for f in _SCHEMA.fields} == _PROMPT_NAMES | {"cat"}


def test_prompt_overlay_fields_are_nested():
    for name in _PROMPT_NAMES:
        f = next(x for x in _SCHEMA.fields if x.name == name)
        assert isinstance(f, NestedField)
        assert f.schema.factory is PromptOverlay


def test_cat_field_is_nested_cat_tool_config():
    assert _cat_schema().factory is CatToolConfig


# Структура вложенной CatToolConfig-схемы

def test_cat_schema_has_max_lines_and_prompt():
    names = {ff.name for ff in _cat_schema().fields}
    assert names == {"max_lines", "prompt"}


def test_cat_max_lines_default_and_parses_string_int():
    max_lines = next(ff for ff in _cat_schema().fields if ff.name == "max_lines")
    assert isinstance(max_lines, FieldSpec)
    assert max_lines.coercer.apply(MISSING) == 2000
    assert max_lines.coercer.apply(StringValue("555")) == 555


def test_cat_max_lines_min_value_enforced():
    import pytest

    from boba.patterns import ConverterInputError
    from boba.schema.value import IntValue

    max_lines = next(ff for ff in _cat_schema().fields if ff.name == "max_lines")
    assert isinstance(max_lines, FieldSpec)
    with pytest.raises(ConverterInputError):
        max_lines.coercer.apply(IntValue(0))


def test_cat_schema_prompt_is_nested_overlay():
    prompt_f = next(ff for ff in _cat_schema().fields if ff.name == "prompt")
    assert isinstance(prompt_f, NestedField)
    assert prompt_f.schema.factory is PromptOverlay
