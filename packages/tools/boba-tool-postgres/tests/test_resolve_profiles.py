"""SqlExecutorConfig: `profiles`-dict (ключ=target, значение — ссылка
`${postgres.<name>}` резолвит OmegaConf), модель валидирует. IO из модели удалён.

Реальный postgres не нужен — модели валидируются без открытия pool'а.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from boba.tool.pg.executor import SqlExecutorConfig


def test_profile_validated_from_dict() -> None:
    """profiles-dict разворачивается в PostgresConnection с дефолтами."""
    cfg = SqlExecutorConfig.model_validate(
        {
            "profiles": {
                "main": {
                    "host": "db.local",
                    "user": "u",
                    "database": "n1bot",
                    "application_name": "[tool.pg:main]",
                    "statement_timeout_ms": 5000,
                },
            },
        },
    )
    conn = cfg.resolve("main")
    assert conn.host == "db.local"
    assert conn.application_name == "[tool.pg:main]"
    assert conn.statement_timeout_ms == 5000


def test_multiple_profiles_and_targets() -> None:
    cfg = SqlExecutorConfig.model_validate(
        {
            "profiles": {
                "main": {"host": "main.host", "user": "u", "database": "db_main"},
                "audit": {"host": "audit.host", "user": "u", "database": "db_audit"},
            },
        },
    )
    assert cfg.targets() == ["audit", "main"]
    assert cfg.resolve("main").host == "main.host"
    assert cfg.resolve("audit").host == "audit.host"


def test_resolve_unknown_target_raises() -> None:
    cfg = SqlExecutorConfig.model_validate(
        {"profiles": {"main": {"host": "h", "user": "u", "database": "d"}}},
    )
    with pytest.raises(ValueError, match="не в whitelist"):
        cfg.resolve("nonexistent")


def test_empty_profiles_raises() -> None:
    with pytest.raises(ValidationError, match="ни одного профиля"):
        SqlExecutorConfig.model_validate({"profiles": {}})
