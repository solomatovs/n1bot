"""Вход по паролю: порт провайдера и итог входа до строки users и токена.

Ошибки (выпускают реализации):
AuthenticationError — логин не зарегистрирован или пароль неверен.
AuthorizationError — вход запрещён: исключение или ни одной роли.
ExternalServiceError — каталог недоступен.
InternalServiceError — ошибка конфига или каталога на нашей стороне.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, ConfigDict

__all__ = ["PasswordSignIn", "SignedIn"]


class SignedIn(BaseModel):
    """Кто вошёл: ключ строки users, отображаемое имя и metadata входа."""

    model_config = ConfigDict(frozen=True)

    identifier: str
    display_name: str
    metadata: Mapping[str, object]


class PasswordSignIn(Protocol):
    """Провайдер входа по логину и паролю; None — логин провайдеру неизвестен."""

    @abstractmethod
    async def sign_in(self, username: str, password: str) -> SignedIn | None: ...
