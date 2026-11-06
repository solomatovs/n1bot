"""Сборка стандартных способов входа для изолированных тестов."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from pydantic import SecretStr

from boba.auth.assembly import AuthServices
from boba.auth.config import (
    AuthConfig,
    KerberosAuthConfig,
    LdapAuthConfig,
    LocalAuthConfig,
    ProxyAuthConfig,
)
from boba.auth.providers.backends import StandardAuthBackends
from boba.auth.providers.kerberos import SpnegoGate
from boba.auth.providers.password import CompositeSignIn
from boba.auth.signin import SignIns
from boba.chat.profiles import (
    ChatProfiles,
    LocalChatProfileConfig,
    ModelLimits,
    ReasoningReturn,
)
from boba.identity.api import RoleCatalog
from boba.identity.directory import UserDirectory
from boba.identity.signin import PasswordSignIn, ProxySignIn
from boba.identity.sso import SsoAdmission
from boba.ldap import Ldap3Directory
from boba.llm.http.openai import OpenAiProvider
from boba.stand.roles import MemoryRoles
from boba.transport.http import HttpTransportConfig
from boba.transport.http.connection import BearerAuth, HttpConnection

__all__ = ["SignInStand"]


class SignInStand:
    """Профили чата и стандартные реализации провайдеров авторизации."""

    PROFILE: ClassVar[str] = "general"
    FAKE_HOST: ClassVar[str] = "fake-llm"

    @classmethod
    def provider(cls) -> OpenAiProvider:
        return OpenAiProvider(
            kind="openai",
            connection=HttpConnection(
                host=cls.FAKE_HOST,
                path="/v1",
                auth=BearerAuth(method="bearer", token=SecretStr("k")),
            ),
            transport=HttpTransportConfig(),
        )

    @classmethod
    def limits(cls) -> ModelLimits:
        return ModelLimits(
            context_window=32000,
            max_output_tokens=4096,
            reasoning_return=ReasoningReturn.NEVER,
        )

    @classmethod
    def profile(
        cls, *, default: bool = True, roles: list[str] | None = None
    ) -> LocalChatProfileConfig:
        if roles is None:
            roles = ["*"]

        return LocalChatProfileConfig.model_validate(
            {
                "display_name": "Stand",
                "description": "stand profile",
                "default": default,
                "roles": roles,
                "provider": cls.provider(),
                "model": "fake",
                "limits": cls.limits(),
                "system_prompt": "stand",
            }
        )

    @classmethod
    def profiles(cls, roles: list[str] | None = None) -> ChatProfiles:
        return ChatProfiles({cls.PROFILE: cls.profile(roles=roles)})

    @classmethod
    def backends(
        cls,
        profiles: ChatProfiles | None = None,
        roles: RoleCatalog | None = None,
        directory: UserDirectory | None = None,
    ) -> StandAuthBackends:
        if profiles is None:
            profiles = cls.profiles()

        if roles is None:
            roles = MemoryRoles(
                {
                    "read",
                    "wrt",
                    "admin",
                    "adm",
                    "dev",
                    "DEV",
                    "ADM",
                    "GST",
                    "ops",
                    "analyst",
                }
            )

        if directory is None:
            directory = Ldap3Directory()

        return StandAuthBackends(profiles, roles, directory)


class StandAuthBackends:
    """Адаптер фабрик стандартного набора для тестов провайдеров."""

    SECRET: ClassVar[str] = "stand-session-secret"  # noqa: S105 — test signing key

    def __init__(
        self,
        profiles: ChatProfiles,
        roles: RoleCatalog,
        directory: UserDirectory,
    ) -> None:
        self._profiles = profiles
        self._roles = roles
        self._directory = directory
        self._standard = StandardAuthBackends()

    def build(self, configs: Sequence[AuthConfig], secret: str = SECRET) -> SignIns:
        services = AuthServices(self._directory, self._profiles, self._roles, secret)
        return self._standard.registry.build(configs, services)

    def password(self, configs: Sequence[AuthConfig]) -> PasswordSignIn | None:
        services = self._services()
        providers: list[PasswordSignIn] = []
        for config in configs:
            if isinstance(config, LocalAuthConfig):
                contribution = self._standard.local(config, services)
            elif isinstance(config, LdapAuthConfig):
                contribution = self._standard.ldap(config, services)
            else:
                continue

            if contribution.password is not None:
                providers.append(contribution.password)

        if not providers:
            return None

        if len(providers) == 1:
            return providers[0]

        return CompositeSignIn(providers)

    def proxy(self, config: ProxyAuthConfig) -> ProxySignIn:
        contribution = self._standard.proxy(config, self._services())
        if contribution.proxy is None:
            raise ValueError(f"auth backend {config.type!r} does not provide proxy")

        return contribution.proxy

    def sso(self, config: KerberosAuthConfig) -> SsoAdmission:
        contribution = self._standard.kerberos(config, self._services())
        if not isinstance(contribution.sso, SpnegoGate):
            raise ValueError(f"auth backend {config.type!r} does not provide SSO")

        return contribution.sso.sign_in

    def _services(self) -> AuthServices:
        return AuthServices(self._directory, self._profiles, self._roles, self.SECRET)
