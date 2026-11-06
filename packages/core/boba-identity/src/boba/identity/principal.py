"""Личность входа: единый канонический логин.

Ошибки:
ValueError — логин пуст.
ValidationError — поля личности не соответствуют wire-контракту.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from boba.identity.session import Login

__all__ = ["IdentityRule", "UserIdentity"]


class UserIdentity(BaseModel):
    """Подтверждённая личность для токенов и users: канонический логин.

    Провайдер входа создаёт её по IdentityRule; адаптеры передают тот же
    идентификатор между AID и MCP, независимо от способа доставки токена.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    login: Login

    @model_validator(mode="before")
    @classmethod
    def _from_identifier(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        return {"login": value}

    @property
    def identifier(self) -> Login:
        return self.login

    def matches(self, identifier: str) -> bool:
        return self.identifier == identifier


class IdentityRule(BaseModel):
    """Правило провайдера входа: явные псевдонимы канонических логинов.

    Администратор сопоставляет внешние имена каноническим логинам;
    приложение получает общий идентификатор без типа бэкенда.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    aliases: Mapping[Login, Login] = Field(default_factory=dict)

    @field_validator("aliases", mode="before")
    @classmethod
    def _unique_canonical_aliases(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value

        seen: set[Login] = set()
        for name in value:
            if not isinstance(name, str):
                return value

            canonical = Login(name)
            if canonical in seen:
                msg = f"identity aliases repeat canonical login {canonical!r}"
                raise ValueError(msg)

            seen.add(canonical)

        return value

    @model_validator(mode="after")
    def _aliases_are_direct(self) -> Self:
        for source, target in self.aliases.items():
            if source == target:
                continue

            if target in self.aliases:
                msg = (
                    f"identity alias {source!r} points "
                    f"to another alias {target!r}; expected a canonical login"
                )
                raise ValueError(msg)

        return self

    def identity(self, login: Login) -> UserIdentity:
        canonical = self.aliases.get(login, login)

        return UserIdentity(login=canonical)
