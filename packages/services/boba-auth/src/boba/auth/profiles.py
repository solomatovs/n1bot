"""Провайдеры профилей входа: протокол, реализации и композит. Набор профилей
и выбранный для новых чатов профиль считаются на входе, как роли, и уходят в
metadata входа и в токен. Какие реализации собрать, решает bootstrap: по ролям
— у всех типов входа, по заголовку — у proxy.

Ошибки:
AuthorizationError — заголовок назвал профиль, которого нет в [profiles], либо
    выбранный профиль не входит в выданный набор.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Iterator, Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from boba.auth.config import HeaderProfilesConfig
from boba.auth.roles import HeaderAttribute
from boba.chat.profiles import ChatProfiles
from boba.identity.admission import PrincipalFacts
from boba.identity.errors import AuthorizationError

__all__ = [
    "HeaderProfiles",
    "ProfileGrant",
    "ProfileProvider",
    "ProfileProviders",
    "RoleProfiles",
]

logger = logging.getLogger(__name__)


class ProfileGrant(BaseModel):
    """Что провайдер выдал входу: набор профилей и, возможно, выбранный из них."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    granted: frozenset[str] = frozenset()
    selected: str = ""


class ProfileProvider(Protocol):
    """Источник профилей входа по фактам о вошедшем и уже выданным ролям."""

    @abstractmethod
    async def profiles_of(
        self, facts: PrincipalFacts, roles: frozenset[str]
    ) -> ProfileGrant: ...


class RoleProfiles(ProfileProvider):
    """Реализация ProfileProvider по ролям: профили, чьи [profiles.X].roles
    пересекаются с ролями входа; выбора не даёт."""

    def __init__(self, profiles: ChatProfiles) -> None:
        self._profiles = profiles

    async def profiles_of(
        self, facts: PrincipalFacts, roles: frozenset[str]
    ) -> ProfileGrant:
        return ProfileGrant(granted=self._profiles.granted_by_roles(roles))


class HeaderProfiles(ProfileProvider):
    """Реализация ProfileProvider заголовками доверенного бэкенда: набор через
    запятую и, если настроено, выбранный профиль. Имя, которого нет в
    [profiles], — отказ входа, а не молчаливый пропуск."""

    def __init__(self, config: HeaderProfilesConfig, profiles: ChatProfiles) -> None:
        self._config = config
        self._profiles = profiles

    @property
    def header(self) -> str:
        return self._config.name

    async def profiles_of(
        self, facts: PrincipalFacts, roles: frozenset[str]
    ) -> ProfileGrant:
        raw = facts.attributes.get(HeaderAttribute.PROFILES.value, "")
        names = HeaderAttribute.names(raw)
        self._check_known(facts, self._config.name, names)

        selected = ""
        if self._config.selected:
            selected = facts.attributes.get(HeaderAttribute.PROFILE.value, "").strip()
            self._check_known(facts, self._config.selected, frozenset({selected}))

        return ProfileGrant(granted=names, selected=selected)

    def _check_known(
        self, facts: PrincipalFacts, header: str, names: frozenset[str]
    ) -> None:
        unknown = sorted(self._unknown(names))
        if not unknown:
            return

        msg = (
            f"access denied for {facts.label()}: header {header} names profiles "
            f"{unknown} that are not in [profiles]"
        )
        logger.warning("%s", msg)
        raise AuthorizationError(msg)

    def _unknown(self, names: frozenset[str]) -> Iterator[str]:
        for name in names:
            if not name:
                continue

            if self._profiles.known(name):
                continue

            yield name


class ProfileProviders:
    """Провайдеры профилей одного типа входа: объединение наборов и первый
    непустой выбор, который обязан входить в объединение. Состав собирает
    bootstrap приложения."""

    def __init__(self, providers: Sequence[ProfileProvider]) -> None:
        self._providers = list(providers)

    async def granted(
        self, facts: PrincipalFacts, roles: frozenset[str]
    ) -> ProfileGrant:
        names: set[str] = set()
        selected = ""
        for provider in self._providers:
            grant = await provider.profiles_of(facts, roles)
            names.update(grant.granted)
            if not selected:
                selected = grant.selected

        if selected and selected not in names:
            msg = (
                f"access denied for {facts.label()}: selected profile "
                f"{selected!r} is not among the granted {sorted(names)}"
            )
            logger.warning("%s", msg)
            raise AuthorizationError(msg)

        return ProfileGrant(granted=frozenset(names), selected=selected)
