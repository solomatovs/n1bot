"""Профиль Oracle: разбор секции, аргументы connect(), подпись сессии, секрет
в дампе только с контекстом раскрытия."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from boba.connections.base import ClientIdentity
from boba.db.oracle.connection import OracleConfig, PasswordAuth
from boba.toolkit.types import SecretRevealing

RAW = {
    "host": "oracle.example.com",
    "port": 1521,
    "service": "orclpdb1",
    "connect_timeout": 10,
    "call_timeout": 30000,
    "auth": {"method": "password", "user": "scraper", "password": "secret"},
}


class TestOracleConfig:
    def test_parses_section_and_renders_connect_settings(self) -> None:
        profile = OracleConfig.model_validate(RAW)

        assert profile.kind == "oracle"
        assert isinstance(profile.auth, PasswordAuth)
        assert profile.address_prefix() == "oracle.example.com:1521/orclpdb1"
        assert profile.trace() == "auth=password user=scraper"
        assert profile.connect_settings() == {
            "host": "oracle.example.com",
            "port": 1521,
            "service_name": "orclpdb1",
            "tcp_connect_timeout": 10,
            "user": "scraper",
            "password": "secret",
        }

    def test_labeled_puts_client_into_program(self) -> None:
        profile = OracleConfig.model_validate(RAW)
        client = ClientIdentity(application="boba", login="alice", tool="pg_query")

        labeled = profile.labeled(client)

        assert labeled.program == "boba:alice:pg_query"
        assert labeled.connect_settings()["program"] == "boba:alice:pg_query"
        assert "program" not in profile.connect_settings()

    def test_program_is_cut_to_48_bytes(self) -> None:
        profile = OracleConfig.model_validate(RAW)
        client = ClientIdentity(application="a" * 40, login="b" * 40, tool="c")

        labeled = profile.labeled(client)

        assert len(labeled.program.encode("utf-8")) == 48

    @pytest.mark.parametrize(
        "missing", ["host", "port", "service", "connect_timeout", "call_timeout"]
    )
    def test_required_fields(self, missing: str) -> None:
        raw = dict(RAW)
        del raw[missing]

        with pytest.raises(ValidationError):
            OracleConfig.model_validate(raw)

    def test_unknown_auth_method_is_rejected(self) -> None:
        raw = dict(RAW)
        raw["auth"] = {"method": "kerberos_keytab", "user": "x"}

        with pytest.raises(ValidationError):
            OracleConfig.model_validate(raw)

    def test_password_is_masked_unless_revealed(self) -> None:
        profile = OracleConfig.model_validate(RAW)
        assert isinstance(profile.auth.password, SecretStr)

        masked = profile.model_dump(mode="json")
        assert masked["auth"]["password"] is None

        revealed = profile.model_dump(
            mode="json", context={SecretRevealing.REVEAL_CONTEXT: True}
        )
        assert revealed["auth"]["password"] == "secret"
