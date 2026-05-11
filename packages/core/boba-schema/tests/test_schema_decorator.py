"""Тесты декоратора @schema(invariants=...) и его интеграции с schema_from_dataclass."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import pytest

from boba.patterns import ConverterInputError
from boba.schema import schema, schema_from_dataclass
from boba.schema.coercion import ParseString, RequiredWhen
from boba.schema.coercion.invariants import Ordered
from boba.schema.declaration import ObjectSchema, _PassDictCoercer
from boba.schema.decorator import INVARIANTS_ATTR

# Базовое поведение


def test_decorator_attaches_invariants_to_class():
    inv = RequiredWhen("auth_method", "basic", "auth_user")

    @schema(invariants=inv)
    @dataclass(frozen=True)
    class Cfg:
        auth_method: Annotated[str, ParseString()] = "pat"
        auth_user: Annotated[str, ParseString()] = ""

    assert getattr(Cfg, INVARIANTS_ATTR) is inv


def test_schema_from_dataclass_uses_invariants_from_decorator():
    inv = RequiredWhen("auth_method", "basic", "auth_user")

    @schema(invariants=inv)
    @dataclass(frozen=True)
    class Cfg:
        auth_method: Annotated[str, ParseString()] = "pat"
        auth_user: Annotated[str, ParseString()] = ""

    s = schema_from_dataclass(Cfg)
    assert s.invariants is inv


def test_without_decorator_invariants_is_default_passthrough():
    @dataclass(frozen=True)
    class Cfg:
        x: int = 0

    s = schema_from_dataclass(Cfg)
    assert isinstance(s.invariants, _PassDictCoercer)


def test_decorator_preserves_other_schema_attributes():
    """Декоратор не должен ломать factory/description/fields."""
    inv = Ordered("a", "b")

    @schema(invariants=inv)
    @dataclass(frozen=True)
    class Cfg:
        """Класс с порядковым инвариантом."""

        a: int = 1
        b: int = 2

    s = schema_from_dataclass(Cfg)
    assert s.factory is Cfg
    assert s.description == "Класс с порядковым инвариантом."
    assert len(s.fields) == 2


# Ошибки применения


def test_decorator_on_non_dataclass_raises():
    with pytest.raises(TypeError, match="применим только к dataclass"):

        @schema(invariants=Ordered("a", "b"))
        class Plain:
            pass


def test_decorator_below_dataclass_raises():
    """`@schema` ДОЛЖЕН стоять снаружи `@dataclass`; иначе видит сырой класс."""
    with pytest.raises(TypeError, match="применим только к dataclass"):

        @dataclass(frozen=True)
        @schema(invariants=Ordered("a", "b"))
        class Cfg:
            a: int = 1
            b: int = 2


def test_decorator_requires_keyword_only_invariants():
    with pytest.raises(TypeError):
        schema(Ordered("a", "b"))  # type: ignore[call-arg]


# End-to-end: invariant срабатывает при материализации


def test_invariant_runs_during_materialization():
    from boba.config.bundle import ConfigBundle, FlatConfigMaterializer
    from boba.config.path import ConfigPath
    from boba.config.source.dict import DictSource
    from boba.schema.value import StringValue

    @schema(invariants=RequiredWhen("auth_method", "basic", "auth_user"))
    @dataclass(frozen=True)
    class AuthCfg:
        auth_method: Annotated[str, ParseString()] = "pat"
        auth_user: Annotated[str, ParseString()] = ""

    s = schema_from_dataclass(AuthCfg)

    flat_basic_ok = ConfigBundle.from_sources(
        [
            DictSource(
                {
                    ConfigPath.parse("a.auth_method"): StringValue("basic"),
                    ConfigPath.parse("a.auth_user"): StringValue("alice"),
                },
            ),
        ],
    ).flat
    cfg = FlatConfigMaterializer(s).materialize(flat_basic_ok, ConfigPath.parse("a"))
    assert cfg == AuthCfg(auth_method="basic", auth_user="alice")

    flat_basic_missing = ConfigBundle.from_sources(
        [
            DictSource(
                {ConfigPath.parse("a.auth_method"): StringValue("basic")},
            ),
        ],
    ).flat
    with pytest.raises(ConverterInputError, match="auth_method='basic'"):
        FlatConfigMaterializer(s).materialize(
            flat_basic_missing,
            ConfigPath.parse("a"),
        )


# Совместимость с явной ObjectSchema-конструкцией


def test_schema_attribute_set_on_dataclass_takes_effect():
    """Проверка, что прикрепление атрибута вручную (без декоратора) тоже работает.

    Полезно для unit-тестов и других путей, где @schema нежелателен.
    """

    @dataclass(frozen=True)
    class Cfg:
        a: int = 1
        b: int = 2

    inv = Ordered("a", "b")
    setattr(Cfg, INVARIANTS_ATTR, inv)

    s = schema_from_dataclass(Cfg)
    assert s.invariants is inv


def test_explicit_object_schema_invariants_unchanged():
    """Sanity: ObjectSchema, собранная вручную, не зависит от декоратора."""
    inv = Ordered("a", "b")
    s: ObjectSchema[None] = ObjectSchema(fields=[], invariants=inv)
    assert s.invariants is inv
