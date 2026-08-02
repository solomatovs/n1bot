"""Тесты connections: SecretStr как объявление секрета, шифрование на глубине."""

from __future__ import annotations

import base64
import json
import secrets as std_secrets
from typing import Literal

import pytest
from pydantic import BaseModel, Field, SecretStr

from boba.chainlit2.connections import (
    ConnectionKinds,
    ConnectionsConfig,
    SecretCipher,
    SecretCryptoError,
)
from boba.db.postgres import (
    PostgresConfig,
    PostgresOptionsConfig,
    PostgresPoolConfig,
)
from boba.transport.http import HttpProfile
from boba.transport.http.auth import BasicAuth, BearerAuth, DigestAuth, NoneAuth


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(std_secrets.token_bytes(32)).decode())


def _cipher() -> SecretCipher:
    return SecretCipher(base64.b64decode(_key().get_secret_value()))


def _pg(**kw) -> PostgresConfig:
    return PostgresConfig(
        host="db", user="boba", dbname="n1bot",
        options=PostgresOptionsConfig(), pool=PostgresPoolConfig(), **kw,
    )


class Credentials(BaseModel):
    user: str = ""
    password: SecretStr = SecretStr("")


class Endpoint(BaseModel):
    url: str = ""
    creds: Credentials = Credentials()


class DeepProfile(BaseModel):
    kind: Literal["deep"] = "deep"
    name: str = ""
    endpoint: Endpoint = Endpoint()
    keys: list[SecretStr] = Field(default_factory=list)
    extras: dict[str, SecretStr] = Field(default_factory=dict)


class TestDeepEncryption:
    """Секреты шифруются на любой глубине — это и было главным требованием."""

    @staticmethod
    def _deep() -> DeepProfile:
        return DeepProfile(
            name="x",
            endpoint=Endpoint(
                url="u",
                creds=Credentials(user="u", password=SecretStr("ГЛУБОКИЙ")),
            ),
            keys=[SecretStr("КЛЮЧ-СПИСКА-1"), SecretStr("КЛЮЧ-СПИСКА-2")],
            extras={"api": SecretStr("КЛЮЧ-СЛОВАРЯ")},
        )

    def test_third_level_field_is_encrypted(self) -> None:
        sealed = _cipher().encrypt(self._deep())
        assert SecretCipher.is_encrypted(sealed["endpoint"]["creds"]["password"])

    def test_no_secret_survives_in_plaintext(self) -> None:
        sealed = _cipher().encrypt(self._deep())
        blob = json.dumps(sealed, ensure_ascii=False)
        for secret in ("ГЛУБОКИЙ", "КЛЮЧ-СПИСКА-1", "КЛЮЧ-СПИСКА-2", "КЛЮЧ-СЛОВАРЯ"):
            assert secret not in blob

    def test_structure_and_public_fields_stay_visible(self) -> None:
        sealed = _cipher().encrypt(self._deep())
        assert sealed["name"] == "x"
        assert sealed["endpoint"]["url"] == "u"
        assert sealed["endpoint"]["creds"]["user"] == "u"

    def test_secrets_in_list_are_encrypted(self) -> None:
        sealed = _cipher().encrypt(self._deep())
        assert all(SecretCipher.is_encrypted(k) for k in sealed["keys"])

    def test_secrets_in_dict_are_encrypted(self) -> None:
        sealed = _cipher().encrypt(self._deep())
        assert SecretCipher.is_encrypted(sealed["extras"]["api"])

    def test_roundtrip_restores_every_level(self) -> None:
        cipher = _cipher()
        back = DeepProfile.model_validate(cipher.decrypt(cipher.encrypt(self._deep())))
        assert back.endpoint.creds.password.get_secret_value() == "ГЛУБОКИЙ"
        assert [k.get_secret_value() for k in back.keys] == [
            "КЛЮЧ-СПИСКА-1", "КЛЮЧ-СПИСКА-2",
        ]
        assert back.extras["api"].get_secret_value() == "КЛЮЧ-СЛОВАРЯ"

    def test_foreign_key_cannot_decrypt(self) -> None:
        sealed = _cipher().encrypt(self._deep())
        with pytest.raises(SecretCryptoError):
            _cipher().decrypt(sealed)

    def test_two_encryptions_differ(self) -> None:
        cipher = _cipher()
        assert cipher.encrypt(SecretStr("x")) != cipher.encrypt(SecretStr("x"))

    def test_plain_values_are_untouched(self) -> None:
        assert _cipher().encrypt({"port": 5432, "flag": True}) == {
            "port": 5432,
            "flag": True,
        }

    def test_decrypt_leaves_plain_values(self) -> None:
        assert _cipher().decrypt({"host": "db", "port": 5432}) == {
            "host": "db",
            "port": 5432,
        }


class TestRealProfiles:
    def test_postgres_password_is_a_secret(self) -> None:
        assert isinstance(_pg(password="p").password, SecretStr)

    def test_postgres_password_encrypted(self) -> None:
        sealed = _cipher().encrypt(_pg(password="СЕКРЕТ"))
        assert SecretCipher.is_encrypted(sealed["password"])
        assert "СЕКРЕТ" not in json.dumps(sealed, ensure_ascii=False)

    def test_libpq_still_gets_a_plain_string(self) -> None:
        assert _pg(password="СЕКРЕТ").conn_settings()["password"] == "СЕКРЕТ"

    def test_kind_does_not_leak_into_libpq(self) -> None:
        assert "kind" not in _pg().conn_settings()

    def test_password_is_masked_in_dumps(self) -> None:
        assert "СЕКРЕТ" not in _pg(password="СЕКРЕТ").model_dump_json()

    def test_pool_cache_key_still_separates_passwords(self) -> None:
        first = {**_pg(password="А").conn_settings(), **_pg().pool_settings()}
        second = {**_pg(password="Б").conn_settings(), **_pg().pool_settings()}
        assert json.dumps(first, sort_keys=True, default=str) != json.dumps(
            second, sort_keys=True, default=str,
        )

    @pytest.mark.parametrize(
        "auth",
        [
            NoneAuth(method="none"),
            BasicAuth(method="basic", user="u", password=SecretStr("p4ss")),
            BearerAuth(method="bearer", token=SecretStr("t0ken")),
            DigestAuth(method="digest", user="u", password=SecretStr("p4ss")),
        ],
    )
    def test_web_auth_variants_roundtrip(self, auth) -> None:
        cipher = _cipher()
        sealed = cipher.encrypt(HttpProfile(base_url="http://x", auth=auth))
        blob = json.dumps(sealed, ensure_ascii=False)
        assert "p4ss" not in blob
        assert "t0ken" not in blob
        assert HttpProfile.model_validate(cipher.decrypt(sealed)).auth == auth

    def test_httpx_auth_still_built(self) -> None:
        bearer = BearerAuth(method="bearer", token=SecretStr("t"))
        assert bearer.httpx_auth() is not None

    def test_token_masked_in_dump(self) -> None:
        profile = HttpProfile(
            base_url="http://x",
            auth=BearerAuth(method="bearer", token=SecretStr("TOK")),
        )
        assert "TOK" not in profile.model_dump_json()


class TestConnectionKinds:
    def test_postgres_kind(self) -> None:
        assert ConnectionKinds.model("postgres") is PostgresConfig

    def test_web_kind(self) -> None:
        assert ConnectionKinds.model("web") is HttpProfile

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="неизвестный kind"):
            ConnectionKinds.model("redis")

    def test_kind_of_instance(self) -> None:
        assert ConnectionKinds.kind_of(_pg()) == "postgres"

    def test_kind_is_part_of_the_model(self) -> None:
        assert PostgresConfig.model_fields["kind"].default == "postgres"
        assert HttpProfile.model_fields["kind"].default == "web"

    def test_known_kinds(self) -> None:
        assert ConnectionKinds.known() == ("postgres", "web")


class TestConnectionsConfig:
    def test_key_must_be_base64(self) -> None:
        with pytest.raises(ValueError, match="base64"):
            ConnectionsConfig(encryption_key=SecretStr("не base64!"))

    def test_key_must_be_32_bytes(self) -> None:
        short = SecretStr(base64.b64encode(std_secrets.token_bytes(16)).decode())
        with pytest.raises(ValueError, match="32 байт"):
            ConnectionsConfig(encryption_key=short)

    def test_valid_key_decodes(self) -> None:
        assert len(ConnectionsConfig(encryption_key=_key()).key_bytes()) == 32

    def test_missing_key_raises_on_use(self) -> None:
        with pytest.raises(ValueError, match="encryption_key не задан"):
            ConnectionsConfig().key_bytes()

    def test_missing_connection_raises_on_use(self) -> None:
        with pytest.raises(ValueError, match="connection не задан"):
            ConnectionsConfig(encryption_key=_key()).require_conn()

    def test_defaults(self) -> None:
        cfg = ConnectionsConfig()
        assert cfg.enable is False
        assert (cfg.db_schema, cfg.table) == ("chainlit", "connections")
