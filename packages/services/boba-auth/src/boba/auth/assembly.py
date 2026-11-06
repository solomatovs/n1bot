"""Сборка входов из фабрик, выбранных приложением.

Ошибки:
ValueError — неизвестный способ входа, повтор регистрации или некорректный конфиг.
InternalServiceError — фабрика не смогла собрать вход.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from boba.auth.config import AuthConfig
from boba.auth.providers.password import CompositeSignIn
from boba.auth.signin import SignIns
from boba.core.components import ComponentBuilder, Components
from boba.identity.api import RoleCatalog
from boba.identity.directory import UserDirectory
from boba.identity.errors import BaseError, InternalServiceError
from boba.identity.signin import PasswordSignIn, ProfileCatalog, ProxySignIn
from boba.identity.sso import SessionRefresh, SpnegoExchange


@dataclass(frozen=True)
class AuthServices:
    """Зависимости фабрик входа, переданные корнем приложения."""

    directory: UserDirectory
    profiles: ProfileCatalog
    roles: RoleCatalog
    secret: str


@dataclass(frozen=True)
class AuthContribution:
    """Порты настроенного входа; фабрика возвращает поддерживаемые возможности."""

    password: PasswordSignIn | None = None
    sso: SpnegoExchange | None = None
    proxy: ProxySignIn | None = None
    renewal: SessionRefresh | None = None


class AuthBackends:
    """Собирает входы только из конфигураций и фабрик, выбранных приложением."""

    def __init__(
        self, factories: Iterable[ComponentBuilder[AuthServices, AuthContribution]]
    ) -> None:
        self._components = Components(AuthConfig, "type", factories)

    def model_for(self, raw: Mapping[str, object]) -> type[BaseModel]:
        return self._components.model_for(raw)

    def parse(self, raw: object) -> AuthConfig:
        return self._components.parse(raw)

    def validate(self, configs: Sequence[AuthConfig]) -> None:
        unique: set[str] = set()
        for config in configs:
            if not config.unique:
                continue

            key = config.type
            if key in unique:
                raise ValueError(f"auth backend {config.type!r} is configured twice")

            unique.add(key)

    def build(self, configs: Sequence[AuthConfig], services: AuthServices) -> SignIns:
        self.validate(configs)
        passwords: list[PasswordSignIn] = []
        sso: SpnegoExchange | None = None
        proxy: ProxySignIn | None = None
        renewals: dict[str, SessionRefresh] = {}
        for config in configs:
            contribution = self._build_one(config, services)
            if contribution.password is not None:
                passwords.append(contribution.password)

            if contribution.sso is not None:
                if sso is not None:
                    raise ValueError("auth has multiple SPNEGO handlers")
                sso = contribution.sso

            if contribution.proxy is not None:
                if proxy is not None:
                    raise ValueError("auth has multiple proxy handlers")
                proxy = contribution.proxy

            if contribution.renewal is not None:
                key = config.type
                if key in renewals:
                    raise ValueError(f"auth has multiple refresh handlers for {key!r}")

                renewals[key] = contribution.renewal

        password = None
        if passwords:
            password = CompositeSignIn(passwords)

        return SignIns(password, sso, proxy, renewals)

    def _build_one(
        self, config: AuthConfig, services: AuthServices
    ) -> AuthContribution:
        try:
            return self._components.build(config, services)
        except BaseError:
            raise
        except Exception as exc:
            raise InternalServiceError(
                internal_detail=(
                    f"building auth backend {config.type!r} failed: {exc}"
                ),
                user_detail=None,
            ) from exc
