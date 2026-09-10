"""Секция [connections]: ключ шифрования и подключение проверяются на границе
конфига, а не в момент использования."""

from __future__ import annotations

import base64
import secrets as std_secrets

import pytest
from pydantic import SecretStr

from boba.connection_broker.store import ConnectionsConfig


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(std_secrets.token_bytes(32)).decode())


class TestConnectionsConfig:
    def test_key_must_be_base64(self) -> None:
        with pytest.raises(ValueError, match="base64"):
            ConnectionsConfig(
                db_schema="chainlit", encryption_key=SecretStr("не base64!")
            )

    def test_key_must_be_32_bytes(self) -> None:
        short = SecretStr(base64.b64encode(std_secrets.token_bytes(16)).decode())
        with pytest.raises(
            ValueError, match="expected 32 bytes in base64, got 16 bytes"
        ):
            ConnectionsConfig(db_schema="chainlit", encryption_key=short)

    def test_valid_key_decodes(self) -> None:
        if (
            len(
                ConnectionsConfig(
                    db_schema="chainlit", encryption_key=_key()
                ).key_bytes()
            )
            != 32
        ):
            raise AssertionError("the key must decode to 32 bytes")

    def test_missing_key_raises_on_use(self) -> None:
        with pytest.raises(ValueError, match="encryption_key is not set"):
            ConnectionsConfig(db_schema="chainlit").key_bytes()

    def test_missing_connection_raises_on_use(self) -> None:
        with pytest.raises(ValueError, match="connection is not set"):
            ConnectionsConfig(
                db_schema="chainlit", encryption_key=_key()
            ).require_conn()

    def test_defaults(self) -> None:
        cfg = ConnectionsConfig(db_schema="chainlit")
        if cfg.enable is not False:
            raise AssertionError("cfg.enable is False")
        if cfg.db_schema != "chainlit":
            raise AssertionError('cfg.db_schema == "chainlit"')
