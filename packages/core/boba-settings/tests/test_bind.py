"""bind: секция конфиг-инстанса -> pydantic-модель; сборка ссылками."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from boba.settings import ConfigBuilder, bind


class _Conn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    host: str
    port: int = 5432


class _Pg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    databases: dict[str, _Conn] = {}


def test_bind_plain_section() -> None:
    cfg = ConfigBuilder().add_dict({"a": {"b": {"host": "h", "port": 6000}}}).build()
    got = bind(cfg, "a.b", _Conn)
    if (got.host, got.port) != ("h", 6000):
        raise AssertionError('(got.host, got.port) == ("h", 6000)')


def test_bind_missing_section_uses_defaults() -> None:
    class _Opt(BaseModel):
        x: int = 7

    cfg = ConfigBuilder().add_dict({"a": {}}).build()
    if bind(cfg, "nope.section", _Opt).x != 7:
        raise AssertionError('bind(cfg, "nope.section", _Opt).x == 7')


def test_bind_required_missing_raises() -> None:
    cfg = ConfigBuilder().add_dict({"a": {"port": 6000}}).build()
    with pytest.raises(ValidationError):
        bind(cfg, "a", _Conn)


def test_bind_reference_assembly() -> None:
    cfg = (
        ConfigBuilder()
        .add_dict(
            {
                "postgres": {"main": {"host": "10.0.0.9", "port": 5432}},
                "tool": {"pg": {"databases": {"main": "${postgres.main}"}}},
            }
        )
        .build()
    )
    pg = bind(cfg, "tool.pg", _Pg)
    if pg.databases["main"].host != "10.0.0.9":
        raise AssertionError('pg.databases["main"].host == "10.0.0.9"')
