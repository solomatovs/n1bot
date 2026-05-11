"""Тесты schema_from_dataclass: dataclass → ObjectSchema."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import pytest

from boba.patterns import ConverterInputError, MissingValueError
from boba.schema import Inline, schema_from_dataclass
from boba.schema.coercion import (
    MISSING,
    ChainCoercer,
    Default,
    MaxValue,
    MinValue,
    ParseBool,
    ParseCsvList,
    ParseInt,
    ParseString,
)
from boba.schema.declaration import (
    CollectionField,
    FieldSpec,
    IndexedShape,
    InlineNestedField,
    KeyedShape,
    NestedField,
    ObjectItem,
    ObjectSchema,
    ScalarItem,
)
from boba.schema.value import StringValue


def _field(schema: Any, name: str) -> FieldSpec[Any]:
    for f in schema.fields:
        if f.name == name:
            assert isinstance(f, FieldSpec)
            return f
    msg = f"field {name!r} not found"
    raise AssertionError(msg)


def _coll(schema: Any, name: str) -> CollectionField[Any, Any, Any]:
    for f in schema.fields:
        if f.name == name:
            assert isinstance(f, CollectionField)
            return f
    msg = f"collection field {name!r} not found"
    raise AssertionError(msg)


def _nested(schema: Any, name: str) -> NestedField[Any]:
    for f in schema.fields:
        if f.name == name:
            assert isinstance(f, NestedField)
            return f
    msg = f"nested field {name!r} not found"
    raise AssertionError(msg)


def _inline_nested(schema: Any, name: str) -> InlineNestedField[Any]:
    for f in schema.fields:
        if f.name == name:
            assert isinstance(f, InlineNestedField)
            return f
    msg = f"inline-nested field {name!r} not found"
    raise AssertionError(msg)


# factory и базовые поля


def test_factory_is_dataclass_itself():
    @dataclass(frozen=True)
    class Cfg:
        base_url: str = "http://localhost"

    schema = schema_from_dataclass(Cfg)
    assert schema.factory is Cfg


def test_class_docstring_becomes_schema_description():
    @dataclass(frozen=True)
    class Cfg:
        """Конфиг подключения."""

        host: str = "localhost"

    schema = schema_from_dataclass(Cfg)
    assert schema.description == "Конфиг подключения."


def test_field_without_default_is_required():
    @dataclass(frozen=True)
    class Cfg:
        api_key: str

    f = _field(schema_from_dataclass(Cfg), "api_key")
    with pytest.raises(MissingValueError):
        f.coercer.apply(MISSING)
    assert f.coercer.apply("sk-123") == "sk-123"


def test_field_with_default_uses_default_coercer():
    @dataclass(frozen=True)
    class Cfg:
        max_iterations: int = 20

    f = _field(schema_from_dataclass(Cfg), "max_iterations")
    assert f.coercer.apply(MISSING) == 20
    assert f.coercer.apply(7) == 7


def test_field_with_factory_default():
    @dataclass
    class Cfg:
        items: list[str] = field(default_factory=list)

    f = _coll(schema_from_dataclass(Cfg), "items")
    assert isinstance(f.shape, IndexedShape)


def test_type_guard_rejects_wrong_runtime_value():
    @dataclass(frozen=True)
    class Cfg:
        port: int = 8080

    f = _field(schema_from_dataclass(Cfg), "port")
    with pytest.raises(ConverterInputError):
        f.coercer.apply("not-an-int")


# Annotated[...]: description и доп. coercer


def test_annotated_string_becomes_field_description():
    @dataclass(frozen=True)
    class Cfg:
        base_url: Annotated[str, "OpenAI-совместимый base URL."] = "http://x"

    f = _field(schema_from_dataclass(Cfg), "base_url")
    assert f.description == "OpenAI-совместимый base URL."


def test_annotated_extra_coercer_is_appended():
    @dataclass(frozen=True)
    class Cfg:
        max_iterations: Annotated[int, "Потолок.", MinValue(1), MaxValue(100)] = 20

    f = _field(schema_from_dataclass(Cfg), "max_iterations")
    assert f.description == "Потолок."
    assert f.coercer.apply(50) == 50
    with pytest.raises(ConverterInputError):
        f.coercer.apply(0)
    with pytest.raises(ConverterInputError):
        f.coercer.apply(101)


@dataclass(frozen=True)
class _CfgWithParseBool:
    flag: Annotated[bool, "...", ParseBool()] = False


@dataclass(frozen=True)
class _CfgWithOptionalParseString:
    name: Annotated[str | None, "...", ParseString()] = None


@dataclass(frozen=True)
class _CfgWithCsvScalarOverride:
    tags: Annotated[list[str], "CSV-разделённые теги.", ParseCsvList()] = field(
        default_factory=list,
    )


@dataclass(frozen=True)
class _CfgWithOptionalCsvScalarOverride:
    tags: Annotated[list[str] | None, "...", ParseCsvList()] = None


def test_annotated_parse_coercer_replaces_autogen_type_guard():
    """Если в Annotated есть ValueCoercer (Parse*/Is*), автогенный guard не добавляется."""
    f = _field(schema_from_dataclass(_CfgWithParseBool), "flag")
    # StringValue("true") должен пройти ParseBool (lenient), без IsBool-guard.
    assert f.coercer.apply(StringValue("true")) is True
    assert f.coercer.apply(StringValue("false")) is False


def test_annotated_parse_coercer_works_for_optional():
    """Опт-аут type-guard работает и для Optional через Nullable-цепочку."""
    f = _field(schema_from_dataclass(_CfgWithOptionalParseString), "name")
    assert f.coercer.apply(MISSING) is None
    assert f.coercer.apply(None) is None
    assert f.coercer.apply(StringValue("ok")) == "ok"


def test_list_with_scalar_coercer_becomes_field_spec_not_collection():
    """ValueCoercer на list[T] переключает поле в scalar-pathway (FieldSpec)."""
    schema = schema_from_dataclass(_CfgWithCsvScalarOverride)
    f = _field(schema, "tags")
    # ParseCsvList на StringValue("a,b,c") должен вернуть list.
    assert f.coercer.apply(StringValue("a,b,c")) == ["a", "b", "c"]


def test_optional_list_with_scalar_coercer_supports_missing_and_none():
    schema = schema_from_dataclass(_CfgWithOptionalCsvScalarOverride)
    f = _field(schema, "tags")
    assert f.coercer.apply(MISSING) is None
    assert f.coercer.apply(None) is None
    assert f.coercer.apply(StringValue("x,y")) == ["x", "y"]


def test_list_without_coercer_still_builds_collection():
    """Если нет ValueCoercer — list[T] по-прежнему CollectionField."""

    @dataclass(frozen=True)
    class _Plain:
        items: list[str] = field(default_factory=list)

    c = _coll(schema_from_dataclass(_Plain), "items")
    assert isinstance(c.shape, IndexedShape)


# Optional


def test_optional_without_default_becomes_default_none():
    @dataclass(frozen=True)
    class Cfg:
        proxy_url: str | None = None

    f = _field(schema_from_dataclass(Cfg), "proxy_url")
    assert f.coercer.apply(MISSING) is None
    assert f.coercer.apply(None) is None
    assert f.coercer.apply("http://proxy") == "http://proxy"


def test_optional_rejects_wrong_type():
    @dataclass(frozen=True)
    class Cfg:
        retries: int | None = None

    f = _field(schema_from_dataclass(Cfg), "retries")
    with pytest.raises(ConverterInputError):
        f.coercer.apply("nope")


# Literal


def test_literal_becomes_oneof():
    @dataclass(frozen=True)
    class Cfg:
        log_level: Literal["debug", "info", "warn", "error"] = "info"

    f = _field(schema_from_dataclass(Cfg), "log_level")
    assert f.coercer.apply("debug") == "debug"
    with pytest.raises(ConverterInputError):
        f.coercer.apply("trace")


# Коллекции


def test_list_of_str_is_indexed_collection():
    @dataclass(frozen=True)
    class Cfg:
        tags: tuple[str, ...] = ()  # хранится как tuple, но тип list[str]
        # NOTE: dataclass-поля коллекций обычно с default_factory.

    # фикстура: используем default_factory list для list-поля
    @dataclass
    class Cfg2:
        tags: list[str] = field(default_factory=list)

    c = _coll(schema_from_dataclass(Cfg2), "tags")
    assert isinstance(c.shape, IndexedShape)
    assert isinstance(c.reader, ScalarItem)


def test_dict_str_str_is_keyed_collection():
    @dataclass
    class Cfg:
        headers: dict[str, str] = field(default_factory=dict)

    c = _coll(schema_from_dataclass(Cfg), "headers")
    assert isinstance(c.shape, KeyedShape)


def test_dict_with_non_str_keys_rejected():
    @dataclass
    class Cfg:
        codes: dict[int, str] = field(default_factory=dict)

    with pytest.raises(TypeError, match="str-ключи"):
        schema_from_dataclass(Cfg)


# Nested dataclass


@dataclass(frozen=True)
class _Nested:
    """Вложенная подструктура."""

    inner_value: int = 1


@dataclass(frozen=True)
class _Outer:
    nested: _Nested = field(default_factory=_Nested)
    flag: bool = False


@dataclass(frozen=True)
class _OverrideInner:
    x: int = 1


_OVERRIDE_INNER_SCHEMA: ObjectSchema[_OverrideInner] = ObjectSchema(
    fields=[
        FieldSpec(
            name="x",
            coercer=ChainCoercer(Default(99), ParseInt()),
            description="explicit-override",
        ),
    ],
    factory=_OverrideInner,
    description="explicit",
)


@dataclass(frozen=True)
class _OuterWithInlineOverride:
    inner: Annotated[_OverrideInner, Inline(_OVERRIDE_INNER_SCHEMA)] = field(
        default_factory=_OverrideInner,
    )


def test_nested_dataclass_becomes_nested_field():
    schema = schema_from_dataclass(_Outer)
    n = _nested(schema, "nested")
    assert n.schema.factory is _Nested
    assert n.schema.description == "Вложенная подструктура."


def test_list_of_dataclass_uses_object_item():
    @dataclass
    class Cfg:
        items: list[_Nested] = field(default_factory=list)

    c = _coll(schema_from_dataclass(Cfg), "items")
    assert isinstance(c.shape, IndexedShape)
    assert isinstance(c.reader, ObjectItem)


# Ошибки


def test_non_dataclass_argument_rejected():
    class Plain:
        x: int = 1

    with pytest.raises(TypeError, match="ожидается dataclass-тип"):
        schema_from_dataclass(Plain)


def test_unsupported_field_type_raises():
    @dataclass(frozen=True)
    class Cfg:
        blob: bytes = b""

    with pytest.raises(TypeError, match="неподдерживаемый тип"):
        schema_from_dataclass(Cfg)


def test_optional_dataclass_field_rejected():
    @dataclass(frozen=True)
    class Cfg:
        nested: _Nested | None = None

    with pytest.raises(TypeError, match="Optional пока не поддерживаются"):
        schema_from_dataclass(Cfg)


# ── Inline-вложенный dataclass ────────────────────────────────────────────


def test_inline_marker_produces_inline_nested_field():
    @dataclass(frozen=True)
    class Cfg:
        nested: Annotated[_Nested, Inline()] = field(default_factory=_Nested)

    f = _inline_nested(schema_from_dataclass(Cfg), "nested")
    assert f.schema.factory is _Nested
    assert f.schema.description == "Вложенная подструктура."


def test_inline_with_description_keeps_description():
    @dataclass(frozen=True)
    class Cfg:
        nested: Annotated[_Nested, "пояснение", Inline()] = field(
            default_factory=_Nested,
        )

    f = _inline_nested(schema_from_dataclass(Cfg), "nested")
    assert f.description == "пояснение"


def test_inline_on_non_dataclass_rejected():
    @dataclass(frozen=True)
    class Cfg:
        port: Annotated[int, Inline()] = 8080

    with pytest.raises(TypeError, match="Inline применим только к полю-dataclass"):
        schema_from_dataclass(Cfg)


def test_inline_with_optional_dataclass_rejected():
    @dataclass(frozen=True)
    class Cfg:
        nested: Annotated[_Nested | None, Inline()] = None

    with pytest.raises(TypeError, match="Optional пока не поддерживаются"):
        schema_from_dataclass(Cfg)


def test_inline_schema_override_used_verbatim():
    """Inline(schema=...) — явное переопределение, используется как есть."""
    f = _inline_nested(schema_from_dataclass(_OuterWithInlineOverride), "inner")
    assert f.schema is _OVERRIDE_INNER_SCHEMA
    assert f.schema.description == "explicit"


def test_double_inline_in_annotated_rejected():
    @dataclass(frozen=True)
    class Cfg:
        inner: Annotated[_Nested, Inline(), Inline()] = field(default_factory=_Nested)

    with pytest.raises(TypeError, match="повторный Inline"):
        schema_from_dataclass(Cfg)


# Сценарий: реальный DTO без ручного SCHEMA


def test_materializes_dto_via_generated_schema():
    """Дымовой сценарий: schema_from_dataclass + FlatConfigMaterializer."""
    from boba.config.bundle import (
        ConfigBundle,
        FlatConfigMaterializer,
    )
    from boba.config.path import ConfigPath
    from boba.config.source.dict import DictSource
    from boba.schema.value import StringValue

    @dataclass(frozen=True)
    class OpenAIConfig:
        """OpenAI-совместимый адаптер."""

        base_url: Annotated[str, "Base URL."] = "http://localhost:4000"
        api_key: Annotated[str, "API-ключ."] = "ollama"

    schema = schema_from_dataclass(OpenAIConfig)

    flat = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("openai.base_url"): StringValue(
                        "http://example/v1",
                    ),
                },
            ),
        ],
    ).flat

    cfg = FlatConfigMaterializer(schema).materialize(
        flat,
        ConfigPath.parse("openai"),
    )
    assert isinstance(cfg, OpenAIConfig)
    assert cfg.base_url == "http://example/v1"
    assert cfg.api_key == "ollama"
