"""Unit-тесты `SqlExecutorConfig._resolve_profiles`: преобразование
списка имён `profiles` в `databases: dict[name, PostgresConnection]`
через чтение `[postgres.<name>]` секций.

Реальный postgres не нужен — модели валидируются без открытия pool'а.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from boba.tool.pg.executor import SqlExecutorConfig


def _write_toml(tmp_path: Path, content: str) -> Path:
    f = tmp_path / "config.toml"
    f.write_text(textwrap.dedent(content), encoding="utf-8")
    return f


def _make_cfg(profiles: list[str]) -> SqlExecutorConfig:
    return SqlExecutorConfig.model_validate({"profiles": profiles})


def test_single_profile_resolved_from_postgres_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`profiles = ["main"]` ⇒ databases["main"] заполнено из [postgres.main]."""
    toml = _write_toml(
        tmp_path,
        """
        [postgres.main]
        host = "db.local"
        port = 5432
        user = "u"
        password = "p"
        database = "n1bot"
        application_name = "[tool.pg:main]"
        statement_timeout_ms = 5000
        """,
    )
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    cfg = _make_cfg(["main"])
    conn = cfg.databases["main"]
    assert conn.host == "db.local"
    assert conn.application_name == "[tool.pg:main]"
    assert conn.statement_timeout_ms == 5000


def test_multiple_profiles_resolved_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Каждое имя в profiles резолвится в свою секцию [postgres.<name>]."""
    toml = _write_toml(
        tmp_path,
        """
        [postgres.main]
        host = "main.host"
        user = "u_main"
        database = "db_main"

        [postgres.audit]
        host = "audit.host"
        user = "u_audit"
        database = "db_audit"
        """,
    )
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    cfg = _make_cfg(["main", "audit"])
    assert set(cfg.databases) == {"main", "audit"}
    assert cfg.databases["main"].host == "main.host"
    assert cfg.databases["audit"].host == "audit.host"


def test_missing_profile_section_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Имя в profiles указывает на отсутствующую секцию ⇒ ValueError."""
    toml = _write_toml(
        tmp_path,
        """
        [postgres.main]
        host = "db.local"
        user = "u"
        database = "d"
        """,
    )
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    with pytest.raises(ValueError, match="не найдена или пуста"):
        _make_cfg(["nonexistent"])


def test_empty_profiles_list_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пустой profiles → `_validate` бросает (databases пуст)."""
    toml = _write_toml(tmp_path, "")
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    with pytest.raises(ValueError, match="список профилей пуст"):
        _make_cfg([])


def test_env_override_profiles_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`profiles` берётся напрямую — env-override через TOML+env merge."""
    toml = _write_toml(
        tmp_path,
        """
        [postgres.audit]
        host = "audit.host"
        user = "u"
        database = "d"
        """,
    )
    monkeypatch.setenv("BOBA_CONFIG_PATH", str(toml))
    cfg = _make_cfg(["audit"])
    assert cfg.databases["audit"].host == "audit.host"
