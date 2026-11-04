"""Профили каталога MCP: секреты конкретных соединений и разбор по kind."""

from __future__ import annotations

import base64
import json
import secrets as std_secrets
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from boba.connections.manifest import ConnectionTypes
from boba.connections.stored import (
    StoredConnection,
)
from boba.crypto import SecretCipher
from boba.db.clickhouse.connection import (
    ClickHouseConfig,
    ClickHouseSettingsConfig,
    NoPasswordAuth,
)
from boba.db.postgres.connection import (
    PasswordAuth,
    PostgresConfig,
    PostgresOptionsConfig,
    PostgresPoolConfig,
)
from boba.stand_core.fakes import FakeSecret
from boba.transport.http import HttpxAuth
from boba.transport.http.connection import (
    BasicAuth,
    BearerAuth,
    DigestAuth,
    HttpConnection,
    NoneAuth,
    RetryStatuses,
)


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(std_secrets.token_bytes(32)).decode())


def _cipher() -> SecretCipher:
    return SecretCipher(base64.b64decode(_key().get_secret_value()))


def _pg(**kw) -> PostgresConfig:
    return PostgresConfig(
        host="db",
        dbname="n1bot",
        auth=PasswordAuth(
            method="password", user="boba", password=SecretStr(kw.pop("password", "x"))
        ),
        options=PostgresOptionsConfig(),
        pool=PostgresPoolConfig(),
        **kw,
    )


class TestRealProfiles:
    def test_postgres_password_is_a_secret(self) -> None:
        auth = _pg(password=FakeSecret.DB).auth
        if not isinstance(auth, PasswordAuth) or not isinstance(
            auth.password, SecretStr
        ):
            raise AssertionError("пароль профиля должен быть SecretStr")

    def test_postgres_password_encrypted(self) -> None:
        sealed = _cipher().encrypt(_pg(password=FakeSecret.DB))
        if not (SecretCipher.is_encrypted(sealed["auth"]["password"])):
            raise AssertionError("пароль в дампе не зашифрован")
        if FakeSecret.DB in json.dumps(sealed, ensure_ascii=False):
            raise AssertionError("пароль утёк в зашифрованный дамп")

    def test_libpq_still_gets_a_plain_string(self) -> None:
        if _pg(password=FakeSecret.DB).conn_settings()["password"] != FakeSecret.DB:
            raise AssertionError("libpq должен получать пароль строкой")

    def test_kind_does_not_leak_into_libpq(self) -> None:
        if "kind" in _pg().conn_settings():
            raise AssertionError('"kind" not in _pg().conn_settings()')

    def test_password_is_masked_in_dumps(self) -> None:
        if FakeSecret.DB in _pg(password=FakeSecret.DB).model_dump_json():
            raise AssertionError("пароль виден в model_dump_json")

    def test_pool_cache_key_still_separates_passwords(self) -> None:
        pool = _pg().pool_settings()
        first = {**_pg(password=FakeSecret.DB).conn_settings(), **pool}
        second = {**_pg(password=FakeSecret.DB_OTHER).conn_settings(), **pool}
        if not (
            json.dumps(first, sort_keys=True, default=str)
            != json.dumps(
                second,
                sort_keys=True,
                default=str,
            )
        ):
            raise AssertionError("json.dumps(first, sort_keys=True, default=str) != j…")

    @pytest.mark.parametrize(
        "auth",
        [
            NoneAuth(method="none"),
            BasicAuth(
                method="basic", user="u", password=SecretStr(FakeSecret.HTTP_BASIC)
            ),
            BearerAuth(method="bearer", token=SecretStr(FakeSecret.HTTP_BEARER)),
            DigestAuth(
                method="digest", user="u", password=SecretStr(FakeSecret.HTTP_BASIC)
            ),
        ],
    )
    def test_web_auth_variants_roundtrip(self, auth) -> None:
        cipher = _cipher()
        sealed = cipher.encrypt(HttpConnection(host="x", port=443, auth=auth))
        blob = json.dumps(sealed, ensure_ascii=False)
        if FakeSecret.HTTP_BASIC in blob:
            raise AssertionError("пароль утёк в зашифрованный профиль")
        if FakeSecret.HTTP_BEARER in blob:
            raise AssertionError("токен утёк в зашифрованный профиль")
        if HttpConnection.model_validate(cipher.decrypt(sealed)).auth != auth:
            raise AssertionError(
                "HttpConnection.model_validate(cipher.decrypt(sealed)).…"
            )

    def test_root_model_roundtrips_without_wrapper(self) -> None:
        cipher = _cipher()
        statuses = RetryStatuses({429: 4})
        connection = HttpConnection(
            host="x",
            port=443,
            retry_statuses=statuses,
        )

        sealed = cipher.encrypt(connection)
        stored_statuses = sealed["retry_statuses"]
        if stored_statuses != {429: 4}:
            raise AssertionError(
                "RootModel value must be stored without a root wrapper"
            )

        restored = HttpConnection.model_validate(cipher.decrypt(sealed))
        if restored.retry_statuses != statuses:
            raise AssertionError("RootModel value must survive profile roundtrip")

    def test_httpx_auth_still_built(self) -> None:
        bearer = BearerAuth(method="bearer", token=SecretStr(FakeSecret.HTTP_BEARER))
        if HttpxAuth().of_auth(bearer, "", None) is None:
            raise AssertionError("HttpxAuth.of(bearer) is not None")

    def test_token_masked_in_dump(self) -> None:
        connection = HttpConnection(
            host="x",
            port=443,
            auth=BearerAuth(method="bearer", token=SecretStr(FakeSecret.HTTP_BEARER)),
        )
        if FakeSecret.HTTP_BEARER in connection.model_dump_json():
            raise AssertionError("токен виден в model_dump_json")


class TestConnectionKind:
    def test_kind_of_postgres(self) -> None:
        if _pg().kind != "postgres":
            raise AssertionError("postgres connection must carry kind postgres")

    def test_kind_of_clickhouse(self) -> None:
        connection = ClickHouseConfig(
            host="ch",
            port=8123,
            interface="http",
            auth=NoPasswordAuth(method="no_password", user="boba"),
            settings=ClickHouseSettingsConfig(),
        )
        if connection.kind != "clickhouse":
            raise AssertionError("clickhouse connection must carry kind clickhouse")

    def test_kind_of_web(self) -> None:
        if HttpConnection(host="x", port=443).kind != "web":
            raise AssertionError("http connection must carry kind web")

    def test_kind_is_part_of_the_model(self) -> None:
        if PostgresConfig.model_fields["kind"].default != "postgres":
            raise AssertionError('PostgresConfig.model_fields["kind"].default == "pos…')
        if HttpConnection.model_fields["kind"].default != "web":
            raise AssertionError('HttpConnection.model_fields["kind"].default == "web"')

    def test_stored_profile_is_picked_by_kind(self) -> None:
        connection = {
            "kind": "clickhouse",
            "host": "ch",
            "port": 8123,
            "interface": "http",
            "auth": {"method": "no_password", "user": "boba"},
            "settings": {},
        }
        types = ConnectionTypes.discover()
        stored = StoredConnection(
            id=UUID(int=1), name="x", connection=types.parse(connection)
        )
        if not isinstance(stored.connection, ClickHouseConfig):
            raise AssertionError("profile must be validated by its kind")
        if stored.kind != "clickhouse":
            raise AssertionError("stored.kind must follow the profile")

    def test_stored_connection_without_kind_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="kind"):
            StoredConnection.model_validate(
                {"id": str(UUID(int=1)), "name": "x", "connection": {}}
            )
