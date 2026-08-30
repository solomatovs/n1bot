"""Кто вошёл и какие профили ему видны: шапка страницы и выбор профиля.

Ошибки (HTTP):
401 — вход не сохранён слоем данных.
403 — профиль недоступен ролям пользователя.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import ClassVar
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from boba.chat.profiles import ChatProfileConfig, ChatProfiles
from boba.identity.api import AuthenticatedUser, UserSettingsStore
from boba.identity.context import Scope
from boba.identity.errors import AuthorizationError
from boba.identity.session import UserMetadataField
from boba.messaging import LockToken, MessageBus, StudioProfileChanged
from boba.studio.api.auth import ApiAuth, CurrentUser
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

    id: UUID
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


class ProfileChoice(BaseModel):
    """Выбор профиля studio: имя и сокет вкладки, которая его выбрала."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: str = Field(min_length=1)
    sid: str = ""


UsersSource = Callable[[], UserSettingsStore]
BusSource = Callable[[], MessageBus]


class AccountApi:
    """Обработчики /me, /me/profile и /profiles: выбор профиля хранится на
    пользователе и расходится по его вкладкам через шину.
    """

    TAG: ClassVar[str] = "account"

    def __init__(
        self, profiles: ChatProfiles, users: UsersSource, bus: BusSource
    ) -> None:
        self._profiles = profiles
        self._users = users
        self._bus = bus

    def mount(self, router: APIRouter) -> None:
        router.add_api_route(
            AccountUrl.ME.value, self.me, methods=["GET"], tags=[self.TAG]
        )
        router.add_api_route(
            AccountUrl.PROFILE.value,
            self.set_profile,
            methods=["PUT"],
            tags=[self.TAG],
        )
        router.add_api_route(
            AccountUrl.PROFILES.value,
            self.list_profiles,
            methods=["GET"],
            tags=[self.TAG],
        )

    async def me(self, current_user: CurrentUser, profile: str | None = None) -> Me:
        user = current_user
        chosen = profile
        if chosen is None:
            chosen = await self._stored_profile(user)

        return self._me_of(user, chosen)

    async def set_profile(self, body: ProfileChoice, current_user: CurrentUser) -> Me:
        user = current_user
        if body.profile not in self._profiles.visible_for(user.roles):
            raise AuthorizationError("profile is not available")

        await self._users().set_studio_profile(UUID(user.id), body.profile)
        changed = StudioProfileChanged(profile=body.profile, by_sid=body.sid)
        await self._bus().publish(Scope.user(UUID(user.id)), changed, LockToken.local())

        return self._me_of(user, body.profile)

    def _me_of(self, user: AuthenticatedUser, profile: str | None) -> Me:
        identity = ApiAuth.resolve(user, profile, self._profiles)
        subject = identity.subject

        return Me(
            id=subject.user_id,
            login=subject.login,
            roles=sorted(subject.roles),
            profile=subject.profile,
            sign_in=SignIn.of(user.metadata),
        )

    async def _stored_profile(self, user: AuthenticatedUser) -> str | None:
        """Профиль, выбранный пользователем раньше; недоступный ролям — как невыбранный."""
        stored = await self._users().get_user(user.identifier)
        if stored is None:
            return None

        chosen = stored.metadata.get(UserMetadataField.STUDIO_PROFILE)
        if not isinstance(chosen, str):
            return None

        if chosen not in self._profiles.visible_for(user.roles):
            return None

        return chosen

    async def list_profiles(self, current_user: CurrentUser) -> Sequence[ProfileView]:
        user = current_user

        views: list[ProfileView] = []
        for name, config in self._profiles.visible_for(user.roles).items():
            views.append(ProfileView.of(name, config))

        return views
