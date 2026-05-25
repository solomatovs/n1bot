"""Тесты для `{field}`-плейсхолдеров в `defaults_from`.

Resolver читает значение поля из собственной секции `config_path`
(через `TomlEnvConfigSource.for_path` — TOML+env) и подставляет в
шаблон fallback-пути. Если поле пустое — используется default из
`model_fields`. Если и default пуст — `ValueError`.
"""
# pyright: reportCallIssue=false

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import BaseModel

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict


class _Conn(BaseModel):
    host: str = ""
    port: int = 5432
    user: str = ""


def _write_toml(tmp_path: Path, content: str) -> Path:
    f = tmp_path / "config.toml"
    f.write_text(textwrap.dedent(content), encoding="utf-8")
    return f


# --------------------------------------------------------------------------- #
# Базовый случай: profile задан в TOML, fallback подтянулся
# --------------------------------------------------------------------------- #


class _KbCfg(BobaFlatSettings):
    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb",
        defaults_from=("postgres.{profile}",),
    )
    profile: str = "main"
    conn: _Conn


def test_template_resolves_profile_from_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toml = _write_toml(
        tmp_path,
        """
        [tool.kb]
        profile = "replica"

        [postgres.main]
        host = "primary"
        user = "u_main"

        [postgres.replica]
        host = "replica"
        user = "u_replica"
        """,
    )
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    cfg = _KbCfg()
    assert cfg.profile == "replica"
    assert cfg.conn.host == "replica"
    assert cfg.conn.user == "u_replica"


def test_template_falls_back_to_field_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """profile не задан в TOML → берётся field default ('main')."""
    toml = _write_toml(
        tmp_path,
        """
        [tool.kb]

        [postgres.main]
        host = "primary"
        user = "u_main"
        """,
    )
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    cfg = _KbCfg()
    assert cfg.profile == "main"
    assert cfg.conn.host == "primary"
    assert cfg.conn.user == "u_main"


def test_env_overrides_profile_for_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env-var `BOBA_TOOL__KB__PROFILE` меняет target fallback'а."""
    toml = _write_toml(
        tmp_path,
        """
        [tool.kb]
        profile = "main"

        [postgres.main]
        host = "primary"

        [postgres.audit]
        host = "audit-host"
        """,
    )
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    monkeypatch.setenv("BOBA_TOOL__KB__PROFILE", "audit")
    cfg = _KbCfg()
    assert cfg.profile == "audit"
    assert cfg.conn.host == "audit-host"


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #


class _NoDefault(BobaFlatSettings):
    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb",
        defaults_from=("postgres.{profile}",),
    )
    profile: str = ""  # пустой default
    conn: _Conn


def test_empty_field_and_default_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пустое TOML-значение + пустой default → ValueError из resolver'а."""
    toml = _write_toml(
        tmp_path,
        """
        [tool.kb]
        # profile не задан — будет fallback на default ""
        """,
    )
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    with pytest.raises(ValueError, match="не резолвится"):
        _NoDefault()


# --------------------------------------------------------------------------- #
# Совместимость: defaults_from без плейсхолдеров работает как раньше
# --------------------------------------------------------------------------- #


class _NoTemplate(BobaFlatSettings):
    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb",
        defaults_from=("postgres",),
    )
    conn: _Conn


def test_no_placeholder_works_as_before(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toml = _write_toml(
        tmp_path,
        """
        [postgres]
        host = "shared-host"
        """,
    )
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    cfg = _NoTemplate()
    assert cfg.conn.host == "shared-host"


# --------------------------------------------------------------------------- #
# {section:field}: ссылка на поле в произвольной TOML-секции
# --------------------------------------------------------------------------- #


class _KbCfgFromShared(BobaFlatSettings):
    """Резолв postgres-профиля из общей секции `[kb.storage]`, а не из
    собственной секции config_path. Сценарий: несколько KB-tool'ов
    указывают на один профиль через общий `[kb.storage] profile`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.search.hybrid",
        defaults_from=(
            "kb.storage",
            "postgres.{kb.storage:profile}",
        ),
    )
    conn: _Conn


def test_section_field_template_from_shared(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toml = _write_toml(
        tmp_path,
        """
        [kb.storage]
        profile = "audit"

        [postgres.main]
        host = "main-host"

        [postgres.audit]
        host = "audit-host"
        user = "audit-user"
        """,
    )
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    cfg = _KbCfgFromShared()
    assert cfg.conn.host == "audit-host"
    assert cfg.conn.user == "audit-user"


def test_section_field_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env-override на `[kb.storage] profile` переключает целевой профиль."""
    toml = _write_toml(
        tmp_path,
        """
        [kb.storage]
        profile = "main"

        [postgres.main]
        host = "main-host"

        [postgres.replica]
        host = "replica-host"
        """,
    )
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    monkeypatch.setenv("BOBA_KB__STORAGE__PROFILE", "replica")
    cfg = _KbCfgFromShared()
    assert cfg.conn.host == "replica-host"


def test_section_field_missing_value_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Поле в указанной секции отсутствует → ValueError из resolver'а."""
    toml = _write_toml(
        tmp_path,
        """
        [kb.storage]
        # profile отсутствует
        schema = "public"

        [postgres.main]
        host = "main-host"
        """,
    )
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    with pytest.raises(ValueError, match="не резолвится"):
        _KbCfgFromShared()
