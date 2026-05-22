"""Тесты `BobaFlatSettings` — рекурсивный flat→nested redistribute.

Все тесты unit-уровня: создаём settings-классы напрямую через
`cls(**flat_dict)` без TOML/env источников. Это активирует
`model_validator(mode="before")` и проверяет логику разворота
плоских ключей по дереву submodel-полей.
"""
# pyright: reportCallIssue=false
# Pyright не видит, что `BobaFlatSettings.__init__` принимает flat-ключи
# (это runtime-фича `_redistribute_flat_keys`), поэтому статически
# каждый вызов `cls(host=..., port=...)` выглядит как unknown-arg.

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

# --------------------------------------------------------------------------- #
# Один уровень nesting (regression — старое поведение должно сохраниться)
# --------------------------------------------------------------------------- #


class _Inner(BaseModel):
    a: int
    b: str = "default-b"


class _OneLevel(BobaFlatSettings):
    model_config = BobaSettingsConfigDict(extra="ignore")
    inner: _Inner
    top: int = 0


def test_one_level_flat_keys_redistribute() -> None:
    cfg = _OneLevel(a=1, b="hello", top=42)
    assert cfg.inner.a == 1
    assert cfg.inner.b == "hello"
    assert cfg.top == 42


def test_one_level_nested_section_overrides_flat() -> None:
    """Явная nested-секция перекрывает flat-ключи того же submodel."""
    cfg = _OneLevel(a=1, inner={"a": 99, "b": "from-nested"})
    assert cfg.inner.a == 99
    assert cfg.inner.b == "from-nested"


def test_one_level_partial_flat_partial_nested() -> None:
    """Flat-ключ + nested-секция мерджатся; nested перекрывает overlap."""
    cfg = _OneLevel(a=1, b="from-flat", inner={"a": 99})
    assert cfg.inner.a == 99  # из nested
    assert cfg.inner.b == "from-flat"  # из flat


# --------------------------------------------------------------------------- #
# Два уровня nesting — основная фича рекурсии
# --------------------------------------------------------------------------- #


class _Leaf(BaseModel):
    host: str
    port: int = 5432


class _Mid(BaseModel):
    conn: _Leaf
    schema_name: str = "public"


class _TwoLevel(BobaFlatSettings):
    model_config = BobaSettingsConfigDict(extra="ignore")
    mid: _Mid
    top: int = 0


def test_two_level_flat_keys_redistribute() -> None:
    cfg = _TwoLevel(host="db.local", port=5433, schema_name="kb", top=1)
    assert cfg.mid.conn.host == "db.local"
    assert cfg.mid.conn.port == 5433
    assert cfg.mid.schema_name == "kb"
    assert cfg.top == 1


def test_two_level_default_uses_pydantic_default() -> None:
    cfg = _TwoLevel(host="db.local")
    assert cfg.mid.conn.host == "db.local"
    assert cfg.mid.conn.port == 5432  # default из _Leaf


def test_two_level_explicit_nested_overrides_flat() -> None:
    cfg = _TwoLevel(
        host="from-flat",
        mid={"conn": {"host": "from-nested"}},
    )
    assert cfg.mid.conn.host == "from-nested"


# --------------------------------------------------------------------------- #
# Три уровня — убедиться что глубина не ограничена
# --------------------------------------------------------------------------- #


class _L3(BaseModel):
    deep_field: str


class _L2(BaseModel):
    l3: _L3


class _L1(BaseModel):
    l2: _L2


class _ThreeLevel(BobaFlatSettings):
    model_config = BobaSettingsConfigDict(extra="ignore")
    root: _L1


def test_three_level_flat_key_reaches_leaf() -> None:
    cfg = _ThreeLevel(deep_field="found-me")
    assert cfg.root.l2.l3.deep_field == "found-me"


# --------------------------------------------------------------------------- #
# Конфликты имён — должны падать при определении класса
# --------------------------------------------------------------------------- #


def test_conflict_same_leaf_name_in_two_submodels_raises() -> None:
    """Одинаковое leaf-имя в двух submodel-ветках → ValueError при импорте."""

    class _A(BaseModel):
        port: int

    class _B(BaseModel):
        port: int

    with pytest.raises(ValueError, match="flat-key conflicts"):

        class _Conflict(BobaFlatSettings):
            model_config = BobaSettingsConfigDict(extra="ignore")
            a: _A
            b: _B


def test_conflict_message_lists_paths() -> None:
    class _A(BaseModel):
        port: int

    class _B(BaseModel):
        port: int

    with pytest.raises(ValueError, match="flat-key conflicts") as exc_info:

        class _Conflict(BobaFlatSettings):
            model_config = BobaSettingsConfigDict(extra="ignore")
            primary: _A
            secondary: _B

    msg = str(exc_info.value)
    assert "'port'" in msg
    assert "primary.port" in msg
    assert "secondary.port" in msg


def test_conflict_terminal_vs_nested_terminal_raises() -> None:
    """Terminal-поле на верхнем уровне + одноимённое в nested → конфликт."""

    class _Sub(BaseModel):
        token: str

    with pytest.raises(ValueError, match="flat-key conflicts"):

        class _Conflict(BobaFlatSettings):
            model_config = BobaSettingsConfigDict(extra="ignore")
            sub: _Sub
            token: str   # ← конфликтует с sub.token


# --------------------------------------------------------------------------- #
# Submodel-name не конфликтует с leaf-именем
# --------------------------------------------------------------------------- #


class _Cfg(BaseModel):
    host: str


class _SubmodelNameIsNotLeaf(BobaFlatSettings):
    """`cfg` — имя submodel-поля, `host` — leaf. Имена не пересекаются."""

    model_config = BobaSettingsConfigDict(extra="ignore")
    cfg: _Cfg


def test_submodel_name_is_not_a_flat_key() -> None:
    """Имя самого submodel-поля не должно считаться leaf-ключом."""
    parsed = _SubmodelNameIsNotLeaf(host="x")
    assert parsed.cfg.host == "x"


# --------------------------------------------------------------------------- #
# extra-fields на корне не ломают разбор
# --------------------------------------------------------------------------- #


class _ExtraIgnore(BobaFlatSettings):
    model_config = BobaSettingsConfigDict(extra="ignore")
    inner: _Inner


def test_unknown_keys_ignored() -> None:
    cfg = _ExtraIgnore(a=1, b="x", unknown_extra="dropped")
    assert cfg.inner.a == 1
    assert cfg.inner.b == "x"


# --------------------------------------------------------------------------- #
# Default-factory submodel'ов работает, если ничего не передано
# --------------------------------------------------------------------------- #


class _Defaultable(BaseModel):
    a: int = 7
    b: str = "default"


class _AllDefaults(BobaFlatSettings):
    model_config = BobaSettingsConfigDict(extra="ignore")
    inner: _Defaultable = Field(default_factory=_Defaultable)


def test_empty_input_uses_defaults() -> None:
    cfg = _AllDefaults()
    assert cfg.inner.a == 7
    assert cfg.inner.b == "default"
