"""Шифрование секретов модели: SecretStr — объявление, обход — по значениям."""

from __future__ import annotations

import base64
from typing import Any, ClassVar

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, SecretStr

__all__ = ["SecretCipher", "SecretCryptoError"]


class SecretCryptoError(Exception):
    """Значение не расшифровалось: другой ключ или порча данных."""


class SecretCipher:
    """Шифрует все SecretStr модели на любой глубине, обходя значения."""

    PREFIX: ClassVar[str] = "enc:v1:"

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, value: Any) -> Any:
        if isinstance(value, SecretStr):
            return self.PREFIX + self._fernet.encrypt(
                value.get_secret_value().encode(),
            ).decode()
        if isinstance(value, BaseModel):
            return {
                name: self.encrypt(getattr(value, name))
                for name in type(value).model_fields
            }
        if isinstance(value, list | tuple):
            return [self.encrypt(item) for item in value]
        if isinstance(value, dict):
            return {key: self.encrypt(item) for key, item in value.items()}
        return value

    def decrypt(self, value: Any) -> Any:
        if self.is_encrypted(value):
            try:
                return self._fernet.decrypt(value[len(self.PREFIX) :]).decode()
            except (InvalidToken, ValueError) as e:
                msg = "value is not decrypted"
                raise SecretCryptoError(msg) from e
        if isinstance(value, dict):
            return {key: self.decrypt(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.decrypt(item) for item in value]
        return value

    @classmethod
    def is_encrypted(cls, value: Any) -> bool:
        return isinstance(value, str) and value.startswith(cls.PREFIX)
