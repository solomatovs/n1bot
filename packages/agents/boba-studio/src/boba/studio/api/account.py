"""Кто вошёл и какие профили ему видны: шапка страницы и выбор профиля.

Ошибки (HTTP):
401 — вход не сохранён слоем данных.
403 — профиль недоступен ролям пользователя.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import ClassVar

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from boba.chat.profiles import ChatProfileConfig, ChatProfiles
from boba.identity.session import UserMetadataField
from boba.studio.api.auth import ApiIdentity, CurrentUser
from boba.studio.api.urls import AccountUrl

__all__ = ["AccountApi", "Me", "ProfileView", "SignIn"]


class SignIn(BaseModel):
    """Чем выпущен вход: провайдер, принципал SSO и наличие делегированного билета."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    principal: str
    ticket: bool

    @classmethod
    def of(cls, metadata: Mapping[str, object]) -> SignIn:
        provider = metadata.get(UserMetadataField.PROVIDER, "")
        principal = metadata.get(UserMetadataField.PRINCIPAL, "")
        ticket = bool(metadata.get(UserMetadataField.TICKET))

        return cls(provider=str(provider), principal=str(principal), ticket=ticket)


class Me(BaseModel):
    """Субъект входа под выбранным (или умолчательным) профилем."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    login: str
    roles: Sequence[str]
    profile: str
    sign_in: SignIn


class ProfileView(BaseModel):
    """Профиль чата глазами страницы: без промптов, лимитов и бэкенда."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    display_name: str
    description: str
    icon: str
    default: bool
    models: Sequence[str]
    tools: Sequence[str]

    @classmethod
    def of(cls, name: str, config: ChatProfileConfig) -> ProfileView:
        return cls(
            name=name,
            display_name=config.display_name,
            description=config.description,
            icon=config.icon,
            default=config.default,
            models=list(config.models),
            tools=list(config.tools),
        )


class AccountApi:
    """Обработчики /me и /profiles."""

    TAG: ClassVar[str] = "account"

    def __init__(self, profiles: ChatProfiles) -> None:
        self._profiles = profiles

    def mount(self, router: APIRouter) -> None:
        router.add_api_route(
            AccountUrl.ME.value, self.me, methods=["GET"], tags=[self.TAG]
        )
        router.add_api_route(
            AccountUrl.PROFILES.value,
            self.list_profiles,
            methods=["GET"],
            tags=[self.TAG],
        )

    async def me(self, current_user: CurrentUser, profile: str | None = None) -> Me:
        user = ApiIdentity.user_of(current_user)
        identity = ApiIdentity.resolve(user, profile, self._profiles)
        subject = identity.subject

        return Me(
            id=subject.user_id,
            login=subject.login,
            roles=sorted(subject.roles),
            profile=subject.profile,
            sign_in=SignIn.of(user.metadata),
        )

    async def list_profiles(self, current_user: CurrentUser) -> Sequence[ProfileView]:
        user = ApiIdentity.user_of(current_user)

        views: list[ProfileView] = []
        for name, config in self._profiles.visible_for(user.roles).items():
            views.append(ProfileView.of(name, config))

        return views
