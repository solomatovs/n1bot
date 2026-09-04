"""Шифрование секретов модели: SecretStr — объявление, обход — по значениям.

В хранилище едут только значимые поля: дискриминаторы, обязательные поля и
то, что отличается от дефолта модели.
"""

from __future__ import annotations

import base64
from typing import Any, ClassVar, Literal, get_origin

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
            return (
                self.PREFIX
                + self._fernet.encrypt(
                    value.get_secret_value().encode(),
                ).decode()
            )
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

    @classmethod
    def is_encrypted(cls, value: Any) -> bool:
        return isinstance(value, str) and value.startswith(cls.PREFIX)
