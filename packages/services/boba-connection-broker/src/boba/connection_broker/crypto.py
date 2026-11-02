"""Шифрование соединений: Fernet для хранилища, JWE для вызовов MCP.

Хранилища используют SecretCipher; клиент запечатывает профиль ConnectionSeal,
сервер открывает его SealKeys. Форматы моделей принадлежат boba.connections.

Ошибки:
SecretCryptoError — сохранённый секрет повреждён или зашифрован другим ключом.
RefusalError — запечатанное соединение не подходит ключу или имеет неверную форму.
JoseError — ошибка ключа или шифрования JWE.
ValueError — неверный ключ Fernet/JWE или неподходящие данные для шифрования.
"""

from __future__ import annotations

import base64
from enum import StrEnum
from typing import Any, ClassVar, Literal, get_origin

from cryptography.fernet import Fernet, InvalidToken
from joserfc import jwe
from joserfc.errors import JoseError
from joserfc.jwk import ECKey, GuestProtocol
from pydantic import BaseModel, SecretStr, ValidationError

from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import SealedConnection, SealFeature, SealKey
from boba.core.validation import ValidationText
from boba.identity.errors import RefusalError

__all__ = ["ConnectionSeal", "SealKeys", "SecretCipher", "SecretCryptoError"]


class SealAlgorithm(StrEnum):
    """Параметры JWE запечатанного соединения и имена полей его заголовка."""

    KEY_AGREEMENT = "ECDH-ES"
    CONTENT = "A256GCM"
    CURVE = "P-256"
    HEADER_ALG = "alg"
    HEADER_ENC = "enc"
    HEADER_KID = "kid"


class SecretCryptoError(Exception):
    """Значение не расшифровалось: другой ключ или порча данных."""


class SecretCipher:
    """Адаптер Fernet для хранилищ: шифрует SecretStr на любой глубине модели."""

    PREFIX: ClassVar[str] = "enc:v1:"

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, value: Any) -> Any:
        if isinstance(value, SecretStr):
            plaintext = value.get_secret_value().encode()
            ciphertext = self._fernet.encrypt(plaintext).decode()

            return self.PREFIX + ciphertext

        if isinstance(value, BaseModel):
            return self._encrypt_model(value)

        if isinstance(value, list | tuple):
            return [self.encrypt(item) for item in value]

        if isinstance(value, dict):
            return {key: self.encrypt(item) for key, item in value.items()}

        return value

    def _encrypt_model(self, model: BaseModel) -> dict[str, Any]:
        """Минимальный дамп модели: дефолтные поля восстановит валидация."""
        stored: dict[str, Any] = {}
        for name, field in type(model).model_fields.items():
            current = getattr(model, name)

            if get_origin(field.annotation) is Literal:
                stored[name] = self.encrypt(current)
                continue

            if field.is_required():
                stored[name] = self.encrypt(current)
                continue

            if current == field.get_default(call_default_factory=True):
                continue

            stored[name] = self.encrypt(current)

        return stored

    def decrypt(self, value: Any) -> Any:
        if self.is_encrypted(value):
            try:
                return self._fernet.decrypt(value[len(self.PREFIX) :]).decode()
            except (InvalidToken, ValueError) as e:
                msg = (
                    "decrypting a stored secret failed: the ciphertext does not "
                    f"match the configured key or is corrupted "
                    f"({type(e).__name__}: {e})"
                )
                raise SecretCryptoError(msg) from e

        if isinstance(value, dict):
            return {key: self.decrypt(item) for key, item in value.items()}

        if isinstance(value, list | tuple):
            return [self.decrypt(item) for item in value]

        return value

    @staticmethod
    def is_encrypted(value: Any) -> bool:
        if not isinstance(value, str):
            return False

        return value.startswith(SecretCipher.PREFIX)


class ConnectionSeal:
    """Запечатывание соединений открытым ключом сервера на стороне клиента.

    Создаётся клиентом из ключа, который отдал сервер; каждое значение
    получает свой одноразовый ключ согласования, поэтому два запечатывания
    одного профиля не совпадают.
    """

    def __init__(self, key: SealKey) -> None:
        self._kid = key.kid
        self._public = ECKey.import_key(key.model_dump(exclude={"kid"}))

    def seal(self, connection: SealedConnection) -> str:
        protected = {
            SealAlgorithm.HEADER_ALG.value: SealAlgorithm.KEY_AGREEMENT.value,
            SealAlgorithm.HEADER_ENC.value: SealAlgorithm.CONTENT.value,
            SealAlgorithm.HEADER_KID.value: self._kid,
        }
        plaintext = connection.model_dump_json()

        return jwe.encrypt_compact(protected, plaintext, self._public)


class SealKeys:
    """Ключевая пара сервера инструментов для запечатанных соединений.

    Пара создаётся на старте сервера и живёт только в памяти процесса:
    открытую половину сервер отдаёт клиентам (public), закрытой открывает
    присланные значения (open). После перезапуска сервера пара другая, и
    значение, запечатанное прежним ключом, отвергается — клиент берёт новый
    ключ и запечатывает заново.
    """

    SEPARATORS: ClassVar[int] = 4
    """Компактный JWE — пять частей через точку."""

    def __init__(self) -> None:
        self._private = ECKey.generate_key(SealAlgorithm.CURVE.value)
        self._kid = self._private.thumbprint()

    def public(self) -> SealKey:
        jwk = self._private.as_dict(private=False)

        return SealKey.model_validate({"kid": self._kid, **jwk})

    def feature(self) -> SealFeature:
        """Возможность сервера с текущим ключом — для объявления клиентам."""
        return SealFeature(
            alg=SealAlgorithm.KEY_AGREEMENT.value,
            enc=SealAlgorithm.CONTENT.value,
            key=self.public(),
        )

    def sealed(self, value: str) -> bool:
        """Похоже ли значение на запечатанное: форма компактного JWE."""
        return value.count(".") == self.SEPARATORS

    def open(self, sealed: str) -> SealedConnection:
        if not self.sealed(sealed):
            msg = (
                f"opening a sealed connection failed: expected a compact JWE "
                f"sealed with key {self._kid!r}, got a string of "
                f"{len(sealed)} characters that is not one"
            )
            raise RefusalError(ConnectionRefusal.NOT_SEALED, msg)

        try:
            opened = jwe.decrypt_compact(sealed, self._key_of)
        except (JoseError, ValueError) as exc:
            msg = (
                f"opening a sealed connection with key {self._kid!r} failed: "
                f"the value is damaged or sealed for another server: {exc}"
            )
            raise RefusalError(ConnectionRefusal.SEAL_DAMAGED, msg) from exc

        plaintext = opened.plaintext
        if not plaintext:
            plaintext = b""

        try:
            return SealedConnection.model_validate_json(plaintext)
        except ValidationError as exc:
            # from None: в input_value содержимого ездят секреты профиля
            msg = (
                f"opening a sealed connection with key {self._kid!r} failed: "
                f"the content is not a sealed connection: {ValidationText.of(exc)}"
            )
            raise RefusalError(ConnectionRefusal.SEAL_DAMAGED, msg) from None

    def _key_of(self, sealed: GuestProtocol) -> ECKey:
        """Закрытый ключ для значения; чужой kid — отказ до расшифровки."""
        kid = sealed.headers().get(SealAlgorithm.HEADER_KID.value)
        if kid != self._kid:
            msg = (
                f"opening a sealed connection failed: it is sealed with key "
                f"{kid!r}, the server key is {self._kid!r}; the client must "
                "read the current key of the server and seal the connection again"
            )
            raise RefusalError(ConnectionRefusal.SEAL_KEY_UNKNOWN, msg)

        return self._private
