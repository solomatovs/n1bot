"""Общий результат проверки credentials провайдером входа."""

from __future__ import annotations

from dataclasses import dataclass, field

from boba.auth.providers.profiles import ProfileProviders
from boba.auth.providers.roles import RoleProviders
from boba.identity.admission import PrincipalFacts
from boba.identity.authentication import AccessGrant, RenewalKind
from boba.identity.context import Credential, CredentialAbsence, NoUserCredential
from boba.identity.principal import UserIdentity
from boba.identity.signin import SignInResult


@dataclass(frozen=True)
class SignInDetails:
    """Проверенная личность и факты, из которых собирается итог входа."""

    provider: str
    identity: UserIdentity
    facts: PrincipalFacts
    credential: Credential = field(
        default_factory=lambda: NoUserCredential(code=CredentialAbsence.NOT_DELEGATED)
    )
    renewal: RenewalKind = RenewalKind.JWT


class SignInResultFactory:
    """Общий выход провайдера после проверки учётных данных.

    Получает уже подтверждённую личность и факты входа, применяет единые
    правила ролей и профилей и формирует `SignInResult` для общего SessionService.
    """

    def __init__(self, roles: RoleProviders, profiles: ProfileProviders) -> None:
        self._roles = roles
        self._profiles = profiles

    async def create(self, details: SignInDetails) -> SignInResult:
        roles, managed_roles = await self._roles.assignments(
            details.facts, details.identity
        )
        grant = await self._profiles.granted(details.facts, roles)
        return SignInResult(
            identity=details.identity,
            access=AccessGrant(roles=roles, profiles=grant.granted),
            managed_roles=managed_roles,
            credential=details.credential,
            renewal=details.renewal,
            provider=details.provider,
            preferred_profile=grant.selected,
        )
