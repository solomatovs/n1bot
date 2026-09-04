"""Допуск входа: факты о принципале и правило ролей/исключений одного способа входа.

Провайдер входа (пароль, LDAP, SPNEGO) только собирает факты — логин, DN,
группы, SID-ы из PAC; решение «пускать ли и с какими ролями» принимает
RoleRules, одно на все способы.

Ошибки:
AuthorizationError — принципал исключён, без ролей при require_roles либо PAC
    не разобран при настроенных исключениях по SID.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Sequence

from pydantic import BaseModel, ConfigDict, Field, RootModel

from boba.identity.errors import AuthorizationError

__all__ = ["PrincipalFacts", "RoleExcludeConfig", "RoleMappingConfig", "RoleRules"]

logger = logging.getLogger(__name__)


class RoleMappingConfig(RootModel[dict[str, list[str]]]):
    """Фиксированный мапер ключ (логин, группа, DN, SID) → список ролей."""

    def roles_of(self, key: str) -> list[str]:
        return self.root.get(key, [])

    def roles_of_all(self, keys: Iterable[str]) -> Iterator[str]:
        for key in keys:
            yield from self.roles_of(key)


class RoleExcludeConfig(RootModel[list[str]]):
    """Фиксированный список ключей, которым запрещён вход."""

    def excludes(self, key: str) -> bool:
        return key in self.root

    def excludes_any(self, keys: Iterable[str]) -> bool:
        excluded = set(self.root)

        return not excluded.isdisjoint(keys)


class PrincipalFacts(BaseModel):
    """Что известно о входящем: логин каталога, принципал, DN, группы, SID-ы."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    login: str = ""
    principal: str = ""
    dn: str = ""
    member_of: Sequence[str] = ()
    group_sids: Sequence[str] = ()
    pac_parsed: bool = True
    """False — PAC не разобрался: группы неизвестны, исключения по SID не проверить."""

    def label(self) -> str:
        """Кем назвать входящего в журнале."""
        if self.principal:
            return self.principal

        return self.login


class RoleRules(BaseModel):
    """Маппинги и исключения по каждому виду фактов; пустой маппинг — не настроен."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    require_roles: bool = True
    by_login: RoleMappingConfig = Field(default_factory=lambda: RoleMappingConfig({}))
    by_login_ex: RoleExcludeConfig = Field(
        default_factory=lambda: RoleExcludeConfig([])
    )
    by_principal: RoleMappingConfig = Field(
        default_factory=lambda: RoleMappingConfig({})
    )
    by_principal_ex: RoleExcludeConfig = Field(
        default_factory=lambda: RoleExcludeConfig([])
    )
    by_member_of: RoleMappingConfig = Field(
        default_factory=lambda: RoleMappingConfig({})
    )
    by_member_of_ex: RoleExcludeConfig = Field(
        default_factory=lambda: RoleExcludeConfig([])
    )
    by_dn: RoleMappingConfig = Field(default_factory=lambda: RoleMappingConfig({}))
    by_dn_ex: RoleExcludeConfig = Field(default_factory=lambda: RoleExcludeConfig([]))
    by_sid: RoleMappingConfig = Field(default_factory=lambda: RoleMappingConfig({}))
    by_sid_ex: RoleExcludeConfig = Field(default_factory=lambda: RoleExcludeConfig([]))

    def admit(self, facts: PrincipalFacts) -> list[str]:
        """Роли допущенного без повторов, по алфавиту; отказ — AuthorizationError."""
        if self.by_sid_ex.root and not facts.pac_parsed:
            msg = (
                f"access denied for {facts.label()}: the ticket carries no "
                "readable PAC while by_sid_ex exclusions are configured"
            )
            logger.warning("%s", msg)
            raise AuthorizationError(msg)

        if self._excluded(facts):
            msg = (
                f"access denied for {facts.label()}: the principal matches "
                "an exclusion rule (by_principal_ex, by_member_of_ex, by_dn_ex "
                "or by_sid_ex)"
            )
            logger.warning("%s", msg)
            raise AuthorizationError(msg)

        roles = sorted(set(self._matches(facts)))
        if self.require_roles and not roles:
            msg = (
                f"access denied for {facts.label()}: no role mapping matched "
                "the principal, its groups, DN or SIDs while require_roles = true"
            )
            logger.warning("%s", msg)
            raise AuthorizationError(msg)

        return roles

    def merged(self, other: RoleRules) -> RoleRules:
        """Правила с добавленными маппингами другого источника; require_roles — свой."""
        update: dict[str, object] = {}
        for name in type(self).model_fields:
            if name == "require_roles":
                continue

            mine = getattr(self, name)
            theirs = getattr(other, name)
            if isinstance(mine, RoleMappingConfig):
                update[name] = RoleMappingConfig({**mine.root, **theirs.root})
                continue

            update[name] = RoleExcludeConfig([*mine.root, *theirs.root])

        return self.model_copy(update=update)

    def _excluded(self, facts: PrincipalFacts) -> bool:
        if facts.login and self.by_login_ex.excludes(facts.login):
            return True

        if facts.principal and self.by_principal_ex.excludes(facts.principal):
            return True

        if facts.dn and self.by_dn_ex.excludes(facts.dn):
            return True

        if self.by_member_of_ex.excludes_any(facts.member_of):
            return True

        return self.by_sid_ex.excludes_any(facts.group_sids)

    def _matches(self, facts: PrincipalFacts) -> Iterator[str]:
        if facts.login:
            yield from self.by_login.roles_of(facts.login)

        if facts.principal:
            yield from self.by_principal.roles_of(facts.principal)

        if facts.dn:
            yield from self.by_dn.roles_of(facts.dn)

        yield from self.by_member_of.roles_of_all(facts.member_of)
        yield from self.by_sid.roles_of_all(facts.group_sids)
