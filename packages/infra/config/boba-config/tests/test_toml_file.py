"""Конфиг утилиты с подключённым файлом среды: секция [site] приходит из
include, интерполяции файла видят её."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from boba.config import ConfigError, TomlFile, bind_section

SHARED = """
[site]
    developer    = "ss"
    aid_database = "aid_${site.developer}"
    pg_host      = "db.example"
"""

OWN = """
include = "../conf/site.toml"

[env]
    krb = "${env.base}/krb"

[ix.core]
    dbname = "${site.aid_database}"
    host   = "${site.pg_host}"
    krb    = "${env.krb}"
"""


class Core(BaseModel):
    dbname: str
    host: str
    krb: str


def _write(tmp_path: Path, own: str, shared: str) -> Path:
    """debug/ix-core/conf.toml подключает debug/conf/site.toml."""
    (tmp_path / "conf").mkdir()
    (tmp_path / "conf" / "site.toml").write_text(shared, encoding="utf-8")
    (tmp_path / "ix-core").mkdir()
    path = tmp_path / "ix-core" / "conf.toml"
    path.write_text(own, encoding="utf-8")

    return path


def test_section_sees_the_included_site(tmp_path: Path) -> None:
    path = _write(tmp_path, OWN, SHARED)

    core = bind_section(path, "ix.core", Core)

    assert core.dbname == "aid_ss"
    assert core.host == "db.example"
    assert core.krb == f"{tmp_path / 'ix-core'}/krb"


def test_own_keys_override_the_included(tmp_path: Path) -> None:
    own = OWN + '\n[site]\n    pg_host = "own.example"\n'
    path = _write(tmp_path, own, SHARED)

    sections = TomlFile(path).sections()

    assert sections["site"]["pg_host"] == "own.example"
    assert sections["site"]["aid_database"] == "aid_${site.developer}"
    assert "include" not in sections


def test_file_without_include_is_read_as_is(tmp_path: Path) -> None:
    path = tmp_path / "conf.toml"
    path.write_text('[a]\n    b = "${env.base}"\n', encoding="utf-8")

    assert TomlFile(path).sections() == {"a": {"b": "${env.base}"}}


def test_include_must_be_a_path(tmp_path: Path) -> None:
    path = tmp_path / "conf.toml"
    path.write_text("include = 3\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="include expects a path"):
        TomlFile(path).sections()


def test_missing_included_file_is_named(tmp_path: Path) -> None:
    path = tmp_path / "conf.toml"
    path.write_text('include = "absent.toml"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match=r"absent\.toml: expected a readable toml"):
        bind_section(path, "a", Core)


def test_broken_included_file_is_named(tmp_path: Path) -> None:
    path = _write(tmp_path, OWN, "[site\n")

    with pytest.raises(ConfigError, match=r"site\.toml: reading toml failed"):
        TomlFile(path).sections()
