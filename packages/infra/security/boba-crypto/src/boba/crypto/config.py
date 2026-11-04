"""Настройки ключа Fernet для шифрования секретов хранилища.

Ошибки:
ValueError — ключ не содержит 32 байта в base64."""

from __future__ import annotations

from typing import Annotated

from pydantic import (
    Base64Bytes,
    BaseModel,
    ConfigDict,
    Field,
    Secret,
)


class ValueCipherConfig(BaseModel):
    """Ключ шифрования секретов в строках таблицы: секция хранилища, которое
    шифрует значения, — [connections] (соединения пользователей). Ключ приходит
    ссылкой `cipher = "${keys.encryption}"` и проверяется здесь, на границе
    конфига; по нему хранилище создаёт SecretCipher."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    cipher: Secret[Annotated[Base64Bytes, Field(min_length=32, max_length=32)]] = Field(
        description=(
            "Ключ шифрования значений: 32 байта в base64; смена ключа делает "
            "сохранённые секреты нечитаемыми."
        ),
    )
