"""Варианты авторизации Oracle: аргументы connect() каждого, протокол задаёт
вариант, секреты в дампе только с контекстом раскрытия, чужой метод отвергается."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from boba.db.oracle.connection import (
    NetProtocol,
    OracleAuthMethod,
    OracleConfig,
    PasswordAuth,
    WalletAuth,
)
from boba.toolkit.types import SecretRevealing

PROFILE = {
    "host": "db1",
    "port": 2484,
    "service": "orclpdb1",
    "connect_timeout": 10,
    "call_timeout": 30000,
    "arraysize": 2000,
}


def _profile(auth: dict[str, object]) -> OracleConfig:
    return OracleConfig.model_validate({**PROFILE, "auth": auth})


class TestPasswordAuth:
    def test_connect_settings(self) -> None:
        profile = _profile({"method": "password", "user": "app", "password": "s"})

        assert isinstance(profile.auth, PasswordAuth)
        assert profile.auth.method == OracleAuthMethod.PASSWORD
        assert profile.connect_settings() == {
            "host": "db1",
            "port": 2484,
            "service_name": "orclpdb1",
            "tcp_connect_timeout": 10,
            "user": "app",
            "password": "s",
            "protocol": NetProtocol.TCP.value,
        }
        assert profile.trace() == "auth=password user=app"


class TestWalletAuth:
    RAW = {
        "method": "wallet",
        "user": "app",
        "password": "s",
        "wallet_location": "/etc/oracle/wallet",
        "wallet_password": "w",
        "ssl_server_dn_match": True,
    }

    def test_connect_settings_force_tcps(self) -> None:
        profile = _profile(self.RAW)

        assert isinstance(profile.auth, WalletAuth)
        settings = profile.connect_settings()
        assert settings["protocol"] == NetProtocol.TCPS.value
        assert settings["wallet_location"] == "/etc/oracle/wallet"
        assert settings["wallet_password"] == "w"
        assert settings["ssl_server_dn_match"] is True
        assert settings["password"] == "s"
        assert profile.trace() == "auth=wallet user=app"

    def test_secrets_masked_unless_revealed(self) -> None:
        profile = _profile(self.RAW)

        masked = profile.model_dump(mode="json")["auth"]
        assert masked["password"] is None
        assert masked["wallet_password"] is None
        assert masked["wallet_location"] == "/etc/oracle/wallet"

        revealed = profile.model_dump(
            mode="json", context={SecretRevealing.REVEAL_CONTEXT: True}
        )["auth"]
        assert revealed["password"] == "s"
        assert revealed["wallet_password"] == "w"

    @pytest.mark.parametrize(
        "missing", ["wallet_location", "wallet_password", "ssl_server_dn_match"]
    )
    def test_wallet_fields_are_required(self, missing: str) -> None:
        raw = dict(self.RAW)
        del raw[missing]

        with pytest.raises(ValidationError):
            _profile(raw)


def test_unknown_method_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _profile({"method": "kerberos_keytab", "user": "app", "principal": "a@R"})


def test_extra_auth_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _profile({"method": "password", "user": "app", "password": "s", "dsn": "x"})
