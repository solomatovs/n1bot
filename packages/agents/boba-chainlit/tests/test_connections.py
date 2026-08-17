"""Тесты connections: SecretStr как объявление секрета, шифрование на глубине."""

from __future__ import annotations

import base64
import json
import secrets as std_secrets
from typing import Literal

import pytest
from pydantic import BaseModel, Field, SecretStr

from boba.chainlit.connections import (
    ConnectionKinds,
    ConnectionsConfig,
    GrantKinds,
    SecretCipher,
    SecretCryptoError,
)
from boba.chainlit.data.models import Thread, User
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
        host="db",
        user="boba",
        dbname="n1bot",
        options=PostgresOptionsConfig(),
        pool=PostgresPoolConfig(),
        **kw,
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
        if not (SecretCipher.is_encrypted(sealed["endpoint"]["creds"]["password"])):
            raise AssertionError('SecretCipher.is_encrypted(sealed["endpoint"]["creds…')

    def test_no_secret_survives_in_plaintext(self) -> None:
        sealed = _cipher().encrypt(self._deep())
        blob = json.dumps(sealed, ensure_ascii=False)
        for secret in ("ГЛУБОКИЙ", "КЛЮЧ-СПИСКА-1", "КЛЮЧ-СПИСКА-2", "КЛЮЧ-СЛОВАРЯ"):
            if secret in blob:
                raise AssertionError("secret not in blob")

    def test_structure_and_public_fields_stay_visible(self) -> None:
        sealed = _cipher().encrypt(self._deep())
        if sealed["name"] != "x":
            raise AssertionError('sealed["name"] == "x"')
        if sealed["endpoint"]["url"] != "u":
            raise AssertionError('sealed["endpoint"]["url"] == "u"')
        if sealed["endpoint"]["creds"]["user"] != "u":
            raise AssertionError('sealed["endpoint"]["creds"]["user"] == "u"')

    def test_secrets_in_list_are_encrypted(self) -> None:
        sealed = _cipher().encrypt(self._deep())
        if not (all(SecretCipher.is_encrypted(k) for k in sealed["keys"])):
            raise AssertionError('all(SecretCipher.is_encrypted(k) for k in sealed["k…')

    def test_secrets_in_dict_are_encrypted(self) -> None:
        sealed = _cipher().encrypt(self._deep())
        if not (SecretCipher.is_encrypted(sealed["extras"]["api"])):
            raise AssertionError('SecretCipher.is_encrypted(sealed["extras"]["api"])')

    def test_roundtrip_restores_every_level(self) -> None:
        cipher = _cipher()
        back = DeepProfile.model_validate(cipher.decrypt(cipher.encrypt(self._deep())))
        if back.endpoint.creds.password.get_secret_value() != "ГЛУБОКИЙ":
            raise AssertionError("back.endpoint.creds.password.get_secret_value() == …")
        if not (
            [k.get_secret_value() for k in back.keys]
            == [
                "КЛЮЧ-СПИСКА-1",
                "КЛЮЧ-СПИСКА-2",
            ]
        ):
            raise AssertionError('[k.get_secret_value() for k in back.keys] == [ "КЛЮ…')
        if back.extras["api"].get_secret_value() != "КЛЮЧ-СЛОВАРЯ":
            raise AssertionError('back.extras["api"].get_secret_value() == "КЛЮЧ-СЛОВ…')

    def test_foreign_key_cannot_decrypt(self) -> None:
        sealed = _cipher().encrypt(self._deep())
        with pytest.raises(SecretCryptoError):
            _cipher().decrypt(sealed)

    def test_two_encryptions_differ(self) -> None:
        cipher = _cipher()
        if cipher.encrypt(SecretStr("x")) == cipher.encrypt(SecretStr("x")):
            raise AssertionError('cipher.encrypt(SecretStr("x")) != cipher.encrypt(Se…')

    def test_plain_values_are_untouched(self) -> None:
        if not (
            _cipher().encrypt({"port": 5432, "flag": True})
            == {
                "port": 5432,
                "flag": True,
            }
        ):
            raise AssertionError('_cipher().encrypt({"port": 5432, "flag": True}) == …')

    def test_decrypt_leaves_plain_values(self) -> None:
        if not (
            _cipher().decrypt({"host": "db", "port": 5432})
            == {
                "host": "db",
                "port": 5432,
            }
        ):
            raise AssertionError('_cipher().decrypt({"host": "db", "port": 5432}) == …')


class TestRealProfiles:
    def test_postgres_password_is_a_secret(self) -> None:
        if not (isinstance(_pg(password="p").password, SecretStr)):
            raise AssertionError('isinstance(_pg(password="p").password, SecretStr)')

    def test_postgres_password_encrypted(self) -> None:
        sealed = _cipher().encrypt(_pg(password="СЕКРЕТ"))
        if not (SecretCipher.is_encrypted(sealed["password"])):
            raise AssertionError('SecretCipher.is_encrypted(sealed["password"])')
        if "СЕКРЕТ" in json.dumps(sealed, ensure_ascii=False):
            raise AssertionError('"СЕКРЕТ" not in json.dumps(sealed, ensure_ascii=Fal…')

    def test_libpq_still_gets_a_plain_string(self) -> None:
        if _pg(password="СЕКРЕТ").conn_settings()["password"] != "СЕКРЕТ":
            raise AssertionError('_pg(password="СЕКРЕТ").conn_settings()["password"] …')

    def test_kind_does_not_leak_into_libpq(self) -> None:
        if "kind" in _pg().conn_settings():
            raise AssertionError('"kind" not in _pg().conn_settings()')

    def test_password_is_masked_in_dumps(self) -> None:
        if "СЕКРЕТ" in _pg(password="СЕКРЕТ").model_dump_json():
            raise AssertionError('"СЕКРЕТ" not in _pg(password="СЕКРЕТ").model_dump_j…')

    def test_pool_cache_key_still_separates_passwords(self) -> None:
        first = {**_pg(password="А").conn_settings(), **_pg().pool_settings()}
        second = {**_pg(password="Б").conn_settings(), **_pg().pool_settings()}
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
            BasicAuth(method="basic", user="u", password=SecretStr("p4ss")),
            BearerAuth(method="bearer", token=SecretStr("t0ken")),
            DigestAuth(method="digest", user="u", password=SecretStr("p4ss")),
        ],
    )
    def test_web_auth_variants_roundtrip(self, auth) -> None:
        cipher = _cipher()
        sealed = cipher.encrypt(HttpProfile(base_url="https://x", auth=auth))
        blob = json.dumps(sealed, ensure_ascii=False)
        if "p4ss" in blob:
            raise AssertionError('"p4ss" not in blob')
        if "t0ken" in blob:
            raise AssertionError('"t0ken" not in blob')
        if HttpProfile.model_validate(cipher.decrypt(sealed)).auth != auth:
            raise AssertionError("HttpProfile.model_validate(cipher.decrypt(sealed)).…")

    def test_httpx_auth_still_built(self) -> None:
        bearer = BearerAuth(method="bearer", token=SecretStr("t"))
        if bearer.httpx_auth() is None:
            raise AssertionError("bearer.httpx_auth() is not None")

    def test_token_masked_in_dump(self) -> None:
        profile = HttpProfile(
            base_url="https://x",
            auth=BearerAuth(method="bearer", token=SecretStr("TOK")),
        )
        if "TOK" in profile.model_dump_json():
            raise AssertionError('"TOK" not in profile.model_dump_json()')


class TestConnectionKinds:
    def test_postgres_kind(self) -> None:
        if ConnectionKinds.model("postgres") is not PostgresConfig:
            raise AssertionError('ConnectionKinds.model("postgres") is PostgresConfig')

    def test_web_kind(self) -> None:
        if ConnectionKinds.model("web") is not HttpProfile:
            raise AssertionError('ConnectionKinds.model("web") is HttpProfile')

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown connection kind"):
            ConnectionKinds.model("redis")

    def test_kind_of_instance(self) -> None:
        if ConnectionKinds.kind_of(_pg()) != "postgres":
            raise AssertionError('ConnectionKinds.kind_of(_pg()) == "postgres"')

    def test_kind_is_part_of_the_model(self) -> None:
        if PostgresConfig.model_fields["kind"].default != "postgres":
            raise AssertionError('PostgresConfig.model_fields["kind"].default == "pos…')
        if HttpProfile.model_fields["kind"].default != "web":
            raise AssertionError('HttpProfile.model_fields["kind"].default == "web"')

    def test_known_kinds(self) -> None:
        if ConnectionKinds.known() != ("postgres", "web"):
            raise AssertionError('ConnectionKinds.known() == ("postgres", "web")')


class TestConnectionsConfig:
    def test_key_must_be_base64(self) -> None:
        with pytest.raises(ValueError, match="base64"):
            ConnectionsConfig(encryption_key=SecretStr("не base64!"))

    def test_key_must_be_32_bytes(self) -> None:
        short = SecretStr(base64.b64encode(std_secrets.token_bytes(16)).decode())
        with pytest.raises(ValueError, match="32-byte key required"):
            ConnectionsConfig(encryption_key=short)

    def test_valid_key_decodes(self) -> None:
        if len(ConnectionsConfig(encryption_key=_key()).key_bytes()) != 32:
            raise AssertionError("len(ConnectionsConfig(encryption_key=_key()).key_by…")

    def test_missing_key_raises_on_use(self) -> None:
        with pytest.raises(ValueError, match="encryption_key is not set"):
            ConnectionsConfig().key_bytes()

    def test_missing_connection_raises_on_use(self) -> None:
        with pytest.raises(ValueError, match="connection is not set"):
            ConnectionsConfig(encryption_key=_key()).require_conn()

    def test_defaults(self) -> None:
        cfg = ConnectionsConfig()
        if cfg.enable is not False:
            raise AssertionError("cfg.enable is False")
        if (cfg.db_schema, cfg.table) != ("chainlit", "connections"):
            raise AssertionError('(cfg.db_schema, cfg.table) == ("chainlit", "connect…')


class TestGrantKinds:
    def test_known_kinds_are_table_names(self) -> None:
        if GrantKinds.known_src() != ("connections",):
            raise AssertionError('GrantKinds.known_src() == ("connections",)')
        if GrantKinds.known_tgt() != ("roles", "users"):
            raise AssertionError('GrantKinds.known_tgt() == ("roles", "users")')

    def test_validate_passes_known(self) -> None:
        if GrantKinds.validate_src("connections") != "connections":
            raise AssertionError('GrantKinds.validate_src("connections") == "connecti…')
        if GrantKinds.validate_tgt("roles") != "roles":
            raise AssertionError('GrantKinds.validate_tgt("roles") == "roles"')

    def test_validate_src_rejects_unknown(self) -> None:
        with pytest.raises(ValueError, match="unknown grant src_kind"):
            GrantKinds.validate_src("roles")

    def test_validate_tgt_rejects_unknown(self) -> None:
        with pytest.raises(ValueError, match="unknown grant tgt_kind"):
            GrantKinds.validate_tgt("groups")


class TestUserIntId:
    """users.id теперь integer от базы, uuid сохранён как user_uuid."""

    def test_id_is_not_sent_on_insert(self) -> None:
        columns = User.insert_columns().as_string(None)
        if "user_uuid" not in columns:
            raise AssertionError('"user_uuid" in columns')
        if '"id"' in columns:
            raise AssertionError("'\"id\"' not in columns")

    def test_all_columns_still_include_id(self) -> None:
        if '"id"' not in User.all_columns().as_string(None):
            raise AssertionError("'\"id\"' in User.all_columns().as_string(None)")

    def test_persisted_id_is_the_integer(self) -> None:
        user = User(identifier="boba", id=7)
        if user.to_persisted().id != "7":
            raise AssertionError('user.to_persisted().id == "7"')

    def test_user_uuid_is_generated(self) -> None:
        if User(identifier="boba").user_uuid is None:
            raise AssertionError('User(identifier="boba").user_uuid is not None')

    def test_thread_owner_is_int(self) -> None:
        thread = Thread(user_id=7)
        if thread.to_chainlit(None, [], [])["userId"] != "7":
            raise AssertionError('thread.to_chainlit(None, [], [])["userId"] == "7"')

    def test_thread_without_owner(self) -> None:
        if Thread().to_chainlit(None, [], [])["userId"] is not None:
            raise AssertionError('Thread().to_chainlit(None, [], [])["userId"] is None')
