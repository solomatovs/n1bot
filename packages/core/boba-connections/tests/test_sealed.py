"""Запечатанное соединение: клиент шифрует открытым ключом сервера, сервер
открывает своим закрытым; чужое, повреждённое и незапечатанное отвергается."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from joserfc import jwe
from joserfc.jwk import ECKey
from pydantic import SecretStr

from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import ConnectionSeal, SealedConnection, SealKeys
from boba.identity.errors import RefusalError
from boba.toolkit.types import SecretReveal
from boba.transport.http.connection import HttpConnection

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Чистая логика: сессия приложения не нужна."""


def _connection() -> SealedConnection:
    profile = HttpConnection(
        host="wiki.example.com",
        port=443,
        username="svc",
        password=SecretStr("p4ss-secret"),
    )

    return SealedConnection(
        login="ivanov",
        expires_at=NOW + timedelta(hours=1),
        profile=SecretReveal.dumped(profile),
    )


class TestSealAndOpen:
    def test_server_opens_what_the_client_sealed(self) -> None:
        keys = SealKeys()
        sealed = ConnectionSeal(keys.public()).seal(_connection())

        opened = keys.open(sealed)

        if opened != _connection():
            raise AssertionError(f"содержимое дошло целиком: {opened}")

        profile = HttpConnection.model_validate(opened.profile)
        if profile.password is None:
            raise AssertionError(f"секрет профиля доехал: {profile}")
        if profile.password.get_secret_value() != "p4ss-secret":
            raise AssertionError("секрет профиля собрался обратно открытой строкой")

    def test_sealed_value_hides_the_profile(self) -> None:
        keys = SealKeys()
        sealed = ConnectionSeal(keys.public()).seal(_connection())

        for plain in ("p4ss-secret", "wiki.example.com", "ivanov"):
            if plain in sealed:
                raise AssertionError(f"в запечатанном значении читается {plain!r}")

    def test_same_profile_seals_differently_each_time(self) -> None:
        seal = ConnectionSeal(SealKeys().public())

        if seal.seal(_connection()) == seal.seal(_connection()):
            raise AssertionError("у каждого значения свой ключ согласования")

    def test_public_key_carries_no_private_part(self) -> None:
        key = SealKeys().public()

        if set(key.model_dump()) != {"kid", "kty", "crv", "x", "y"}:
            raise AssertionError(f"открытый ключ — только открытые поля: {key}")
        if key.crv != "P-256":
            raise AssertionError(f"кривая ключа: {key}")

    def test_value_sealed_by_a_plain_jwe_library_opens(self) -> None:
        """Клиент без нашего кода запечатывает стандартным JWE по ключу сервера."""
        keys = SealKeys()
        key = keys.public()

        sealed = jwe.encrypt_compact(
            {"alg": "ECDH-ES", "enc": "A256GCM", "kid": key.kid},
            _connection().model_dump_json(),
            ECKey.import_key(key.model_dump(exclude={"kid"})),
        )

        if keys.open(sealed) != _connection():
            raise AssertionError("стандартный JWE открывается так же")

    def test_expiry_is_told_by_the_content(self) -> None:
        connection = _connection()

        if connection.expired(NOW + timedelta(minutes=59)):
            raise AssertionError("до срока значение годно")
        if not connection.expired(NOW + timedelta(hours=1)):
            raise AssertionError("в срок значение уже негодно")


class TestRefusals:
    def test_value_sealed_for_another_server_names_both_keys(self) -> None:
        other = SealKeys()
        keys = SealKeys()
        sealed = ConnectionSeal(other.public()).seal(_connection())

        with pytest.raises(RefusalError) as refused:
            keys.open(sealed)

        if refused.value.kind != ConnectionRefusal.SEAL_KEY_UNKNOWN:
            raise AssertionError(f"причина отказа: {refused.value.kind}")

        text = str(refused.value)
        if other.public().kid not in text or keys.public().kid not in text:
            raise AssertionError(f"отказ называет оба ключа: {text}")
        if "seal the connection again" not in text:
            raise AssertionError(f"отказ подсказывает запечатать заново: {text}")

    @pytest.mark.parametrize("value", ["conn:main", "main", ""])
    def test_plain_reference_is_not_a_sealed_value(self, value: str) -> None:
        with pytest.raises(RefusalError) as refused:
            SealKeys().open(value)

        if refused.value.kind != ConnectionRefusal.NOT_SEALED:
            raise AssertionError(f"причина отказа: {refused.value.kind}")

    def test_damaged_value_is_refused(self) -> None:
        keys = SealKeys()
        sealed = ConnectionSeal(keys.public()).seal(_connection())

        parts = sealed.split(".")
        parts[3] = parts[3][::-1]
        damaged = ".".join(parts)

        with pytest.raises(RefusalError) as refused:
            keys.open(damaged)

        if refused.value.kind != ConnectionRefusal.SEAL_DAMAGED:
            raise AssertionError(f"причина отказа: {refused.value.kind}")

    def test_foreign_content_under_the_right_key_is_refused(self) -> None:
        keys = SealKeys()
        key = keys.public()
        sealed = jwe.encrypt_compact(
            {"alg": "ECDH-ES", "enc": "A256GCM", "kid": key.kid},
            '{"password": "secret"}',
            ECKey.import_key(key.model_dump(exclude={"kid"})),
        )

        with pytest.raises(RefusalError) as refused:
            keys.open(sealed)

        if refused.value.kind != ConnectionRefusal.SEAL_DAMAGED:
            raise AssertionError(f"причина отказа: {refused.value.kind}")
        if "not a sealed connection" not in str(refused.value):
            raise AssertionError(f"отказ называет причину: {refused.value}")
