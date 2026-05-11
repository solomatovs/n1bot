"""Тесты schema_from_dataclass: dataclass → ObjectSchema."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import pytest

from boba.coercion import MISSING, MaxValue, MinValue
from boba.schema import schema_from_dataclass
from boba.declaration import (
    CollectionField,
    FieldSpec,
    IndexedShape,
    KeyedShape,
    NestedField,
    ObjectItem,
    ScalarItem,
)
from boba.patterns import ConverterInputError, MissingValueError


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


# ── factory и базовые поля ────────────────────────────────────────────────


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


# ── Annotated[...]: description и доп. coercer'ы ──────────────────────────


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


# ── Optional ──────────────────────────────────────────────────────────────


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


# ── Literal ───────────────────────────────────────────────────────────────


def test_literal_becomes_oneof():
    @dataclass(frozen=True)
    class Cfg:
        log_level: Literal["debug", "info", "warn", "error"] = "info"

    f = _field(schema_from_dataclass(Cfg), "log_level")
    assert f.coercer.apply("debug") == "debug"
    with pytest.raises(ConverterInputError):
        f.coercer.apply("trace")


# ── Коллекции ─────────────────────────────────────────────────────────────


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


# ── Nested dataclass ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Nested:
    """Вложенная подструктура."""

    inner_value: int = 1


@dataclass(frozen=True)
class _Outer:
    nested: _Nested = field(default_factory=_Nested)
    flag: bool = False


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


# ── Ошибки ────────────────────────────────────────────────────────────────


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


# ── Сценарий: реальный DTO без ручного SCHEMA ─────────────────────────────


def test_materializes_dto_via_generated_schema():
    """Дымовой сценарий: schema_from_dataclass + FlatConfigMaterializer."""
    from boba.config.bundle import (  # noqa: PLC0415
        ConfigBundle,
        FlatConfigMaterializer,
    )
    from boba.config.path import ConfigPath  # noqa: PLC0415
    from boba.config.source.dict import DictSource  # noqa: PLC0415
    from boba.value import StringValue  # noqa: PLC0415

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
