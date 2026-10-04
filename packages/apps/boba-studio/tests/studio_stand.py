"""Стенд api: роли и профили из конфига studio, приложение api над входами
стенда с входом по одному токену.

Имя модуля своё: conftest.py у пакетов сталкиваются в общей сессии pytest."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import ClassVar
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from boba.chat.profiles import ChatProfiles
from boba.identity.api import AuthenticatedUser
from boba.identity.session import Login
from boba.identity.signin import SignInMetadata
from boba.runtime.config import StudioRuntimeConfig
from boba.runtime.refs import RuntimeRefs
from boba.stand_core.auth import NoUsers, StubAuthenticator
from boba.studio.api.account import UsersSource
from boba.studio.api.app import ApiAccess, ApiApp, ApiExtras
from boba.studio.api.signin import SignInWiring
from boba.studio.api.urls import ApiVersion


class StandProfiles:
    """Роли и профили стенда из конфига studio."""

    @staticmethod
    def roles(config: StudioRuntimeConfig) -> list[str]:
        return sorted(config.roles)

    @staticmethod
    def profiles(config: StudioRuntimeConfig) -> ChatProfiles:
        return ChatProfiles(config.profiles)

    @classmethod
    def profile(cls, config: StudioRuntimeConfig) -> str:
        """Первый профиль, выдаваемый ролям стенда."""
        granted = cls.profiles(config).granted_by_roles(frozenset(cls.roles(config)))
        names = sorted(granted)
        if not names:
            roles = sorted(cls.roles(config))
            configured = sorted(config.profiles)
            msg = (
                f"stand config: no profile among {configured} is visible "
                f"to stand roles {roles}"
            )
            raise RuntimeError(msg)

        return names[0]

    @classmethod
    def user(
        cls, config: StudioRuntimeConfig, extra_roles: Iterable[str] = ()
    ) -> AuthenticatedUser:
        """Пользователь стенда со всеми ролями конфига и профилями по ним, как
        их выдал бы вход."""
        roles = frozenset([*sorted(config.roles), *extra_roles])
        profiles = cls.profiles(config).granted_by_roles(roles)

        return AuthenticatedUser(
            id=uuid4(),
            identifier=Login("user-1"),
            sign_in=SignInMetadata(roles=roles, profiles=profiles),
        )

    @classmethod
    def with_roles(
        cls, config: StudioRuntimeConfig, user: AuthenticatedUser, roles: Iterable[str]
    ) -> AuthenticatedUser:
        """Тот же пользователь с другими ролями и профилями по ним."""
        given = frozenset(roles)
        profiles = cls.profiles(config).granted_by_roles(given)

        return user.model_copy(
            update={"sign_in": SignInMetadata(roles=given, profiles=profiles)}
        )


class ApiStand:
    """Приложение api studio на стенде: вход по токену стенда, входы
    приложения — из refs, пользователь входа подменяется на каждый клиент."""

    BASE_URL: ClassVar[str] = "http://api"

    def __init__(
        self,
        refs: RuntimeRefs,
        profiles: ChatProfiles,
        signin: SignInWiring | None = None,
        extras: ApiExtras | None = None,
        users: UsersSource | None = None,
    ) -> None:
        self.sign_in = StubAuthenticator(None)
        if users is None:
            users = NoUsers.source

        access = ApiAccess(self.sign_in, StubAuthenticator.COOKIE, users)
        self.app: FastAPI = ApiApp.build(refs, access, profiles, signin, extras)

    def client(self, user: AuthenticatedUser | None) -> AsyncClient:
        """Клиент от имени user; None — без cookie входа."""
        self.sign_in.user = user
        cookies: dict[str, str] = {}
        if user is not None:
            cookies = StubAuthenticator.cookies()

        transport = ASGITransport(app=self.app)
        return AsyncClient(transport=transport, base_url=self.BASE_URL, cookies=cookies)

    @staticmethod
    def api_url(path: StrEnum, **params: object) -> str:
        """Путь ресурса под версией api с подставленными параметрами."""
        return ApiVersion.V1.value + path.value.format(**params)
