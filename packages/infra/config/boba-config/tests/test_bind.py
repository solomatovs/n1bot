"""bind: секция дерева конфига -> pydantic-модель; сборка ссылками."""

from __future__ import annotations

import pytest
from omegaconf import DictConfig, OmegaConf
from pydantic import BaseModel, ConfigDict

from boba.config import ConfigError, bind


class _Conn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    host: str
    port: int


class _Pg(BaseModel):
    model_config = ConfigDict(extra="ignore")
    databases: dict[str, _Conn]


def _tree(data: dict[str, object]) -> DictConfig:
    return OmegaConf.create(data)


def test_bind_plain_section() -> None:
    cfg = _tree({"a": {"b": {"host": "h", "port": 6000}}})
    got = bind(cfg, "a.b", _Conn)
    if (got.host, got.port) != ("h", 6000):
        raise AssertionError('(got.host, got.port) == ("h", 6000)')


def test_bind_missing_section_is_refused() -> None:
    cfg = _tree({"a": {}})
    with pytest.raises(ConfigError, match=r"section \[nope\.section\] is missing"):
        bind(cfg, "nope.section", _Conn)


def test_bind_section_not_fitting_the_model_is_refused() -> None:
    cfg = _tree({"a": {"port": 6000}})
    with pytest.raises(ConfigError, match=r"(?s)section \[a\] does not fit _Conn.*host"):
        bind(cfg, "a", _Conn)


def test_bind_reference_assembly() -> None:
    cfg = _tree(
        {
            "postgres": {"main": {"host": "10.0.0.9", "port": 5432}},
            "tool": {"pg": {"databases": {"main": "${postgres.main}"}}},
        }
    )
    pg = bind(cfg, "tool.pg", _Pg)
    if pg.databases["main"].host != "10.0.0.9":
        raise AssertionError('pg.databases["main"].host == "10.0.0.9"')
