"""SecretCipher: шифрование SecretStr во вложенных моделях и контейнерах."""

from __future__ import annotations

import base64
import json
import secrets as std_secrets
from typing import Literal

import pytest
from cryptography.fernet import Fernet
from pydantic import BaseModel, Field, SecretStr

from boba.crypto import SecretCipher, SecretCryptoError
from boba.stand_core.fakes import FakeSecret


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(std_secrets.token_bytes(32)).decode())


def _cipher() -> SecretCipher:
    return SecretCipher(base64.b64decode(_key().get_secret_value()))


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

    def test_existing_fernet_value_opens_with_the_configured_key(self) -> None:
        key = std_secrets.token_bytes(32)
        fernet = Fernet(base64.urlsafe_b64encode(key))
        ciphertext = fernet.encrypt(FakeSecret.DB.encode()).decode()
        stored = {"password": "enc:v1:" + ciphertext}

        opened = SecretCipher(key).decrypt(stored)

        assert opened == {"password": FakeSecret.DB}

    def test_saved_value_keeps_the_fernet_format_and_prefix(self) -> None:
        key = std_secrets.token_bytes(32)
        fernet = Fernet(base64.urlsafe_b64encode(key))

        stored = SecretCipher(key).encrypt(SecretStr(FakeSecret.DB))

        assert stored.startswith("enc:v1:")
        ciphertext = stored.removeprefix("enc:v1:").encode()
        assert fernet.decrypt(ciphertext).decode() == FakeSecret.DB

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
