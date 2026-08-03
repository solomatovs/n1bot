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
    assert (got.host, got.port) == ("h", 6000)


def test_bind_missing_section_uses_defaults() -> None:
    class _Opt(BaseModel):
        x: int = 7

    cfg = ConfigBuilder().add_dict({"a": {}}).build()
    assert bind(cfg, "nope.section", _Opt).x == 7


def test_bind_required_missing_raises() -> None:
    cfg = ConfigBuilder().add_dict({"a": {"port": 6000}}).build()
    with pytest.raises(ValidationError):
        bind(cfg, "a", _Conn)


def test_bind_reference_assembly() -> None:
    cfg = ConfigBuilder().add_dict(
        {
            "postgres": {"main": {"host": "172.18.0.9", "port": 5432}},
            "tool": {"pg": {"databases": {"main": "${postgres.main}"}}},
        }
    ).build()
    pg = bind(cfg, "tool.pg", _Pg)
    assert pg.databases["main"].host == "172.18.0.9"


