"""Личность входа: пространство имён и канонический логин.

Ошибки:
ValueError — идентификатор не содержит пространства имён и логина.
ValidationError — поля личности не соответствуют wire-контракту.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from boba.identity.session import Login

__all__ = ["IdentityRealm", "IdentityRule", "UserIdentity"]

IdentityRealm = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]*$")]


class UserIdentity(BaseModel):
    """Подтверждённая личность для токенов и users: пространство и логин.

    Провайдер входа создаёт её по IdentityRule; адаптеры передают тот же
    идентификатор между AID и MCP, независимо от способа доставки токена.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    realm: IdentityRealm
    login: Login

    @model_validator(mode="before")
    @classmethod
    def _from_identifier(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        realm, separator, login = value.partition(":")
        if not separator:
            msg = (
                f"user identity {value!r}: expected realm:login; "
                "a bare login carries no identity origin"
            )
            raise ValueError(msg)

        return {"realm": realm, "login": login}

    @property
    def identifier(self) -> Login:
        return Login(f"{self.realm}:{self.login}")

    def matches(self, identifier: str) -> bool:
        return self.identifier == identifier


class IdentityRule(BaseModel):
    """Правило провайдера входа: пространство имён и явные псевдонимы логинов.

    Администратор связывает бэкенды одинаковым realm и псевдонимами;
    приложение получает общий идентификатор без типа бэкенда.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    realm: IdentityRealm
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
                    f"identity realm {self.realm!r}: alias {source!r} points "
                    f"to another alias {target!r}; expected a canonical login"
                )
                raise ValueError(msg)

        return self

    def identity(self, login: Login) -> UserIdentity:
        canonical = self.aliases.get(login, login)

        return UserIdentity(realm=self.realm, login=canonical)
