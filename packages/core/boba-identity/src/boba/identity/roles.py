"""Правила ролей входа: статические маппинги логин/атрибут → роли и исключения.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, Field, RootModel

__all__ = [
    "DnExcludeUserProvider",
    "DnUserRolesProvider",
    "LdapRolesConfig",
    "LocalExcludeUserProvider",
    "LocalUserRolesProvider",
    "MemberOfExcludeUserProvider",
    "MemberOfUserRolesProvider",
    "RoleExcludeConfig",
    "RoleMappingConfig",
    "SAMAccountNameExcludeUserProvider",
    "SAMAccountNameUserRolesProvider",
    "SidExcludeUserProvider",
    "SidUserRolesProvider",
]


class RoleMappingConfig(RootModel[dict[str, list[str]]]):
    """Фиксированный мапер пользователь - список ролей"""

    def roles_of(self, key: str) -> list[str]:
        return self.root.get(key, [])


class RoleExcludeConfig(RootModel[list[str]]):
    """Фиксированный список исключённых пользователей/ролей"""

    def exclude_of(self, key: str) -> Iterable[bool]:
        for x in self.root:
            yield x == key


class LocalUserRolesProvider:
    """Локальный провайдер пользователь - список ролей"""

    def __init__(self, mapping: RoleMappingConfig):
        self._mapping = mapping

    def roles_of(self, username: str) -> Iterable[str]:
        yield from self._mapping.roles_of(username)


class LocalExcludeUserProvider:
    """Локальный список пользователей, которым запрещён вход"""

    def __init__(self, mapping: RoleExcludeConfig):
        self._mapping = mapping

    def exclude_of(self, username: str) -> Iterable[bool]:
        yield from self._mapping.exclude_of(username)


class SAMAccountNameUserRolesProvider:
    """Мапер sAMAccountName - список ролей"""

    def __init__(self, mapping: RoleMappingConfig):
        self._mapping = mapping

    def roles_of(self, samaccountname: str) -> Iterable[str]:
        yield from self._mapping.roles_of(samaccountname)


class SAMAccountNameExcludeUserProvider:
    """Список sAMAccountName, которым запрещён вход"""

    def __init__(self, mapping: RoleExcludeConfig):
        self._mapping = mapping

    def exclude_of(self, samaccountname: str) -> Iterable[bool]:
        yield from self._mapping.exclude_of(samaccountname)


class MemberOfUserRolesProvider:
    """Мапер групп memberOf - список ролей"""

    def __init__(self, mapping: RoleMappingConfig):
        self._mapping = mapping

    def roles_of(self, member_of: list[str]) -> Iterable[str]:
        for m in member_of:
            yield from self._mapping.roles_of(m)


class MemberOfExcludeUserProvider:
    """Список групп memberOf, членам которых запрещён вход"""

    def __init__(self, mapping: RoleExcludeConfig):
        self._mapping = mapping

    def exclude_of(self, member_of: list[str]) -> Iterable[bool]:
        for m in member_of:
            yield from self._mapping.exclude_of(m)


class DnUserRolesProvider:
    """Мапер DN пользователя - список ролей"""

    def __init__(self, mapping: RoleMappingConfig):
        self._mapping = mapping

    def roles_of(self, dn: str) -> Iterable[str]:
        return self._mapping.roles_of(dn)


class DnExcludeUserProvider:
    """Список DN пользователей, которым запрещён вход"""

    def __init__(self, mapping: RoleExcludeConfig):
        self._mapping = mapping

    def exclude_of(self, dn: str) -> Iterable[bool]:
        yield from self._mapping.exclude_of(dn)


class LdapRolesConfig(BaseModel):
    samaccountname: RoleMappingConfig | None = Field(
        default=None,
        description="",
    )
    samaccountname_ex: RoleExcludeConfig | None = Field(
        default=None,
        description="Логины, которым запрещён вход (403).",
    )
    member_of: RoleMappingConfig | None = Field(
        default=None,
        description="",
    )
    member_of_ex: RoleExcludeConfig | None = Field(
        default=None,
        description="Группы, членам которых запрещён вход (403).",
    )
    dn: RoleMappingConfig | None = Field(
        default=None,
        description="",
    )
    dn_ex: RoleExcludeConfig | None = Field(
        default=None,
        description="DN пользователей, которым запрещён вход (403).",
    )


class SidUserRolesProvider:
    """Мапер SID группы - список ролей"""

    def __init__(self, mapping: RoleMappingConfig):
        self._mapping = mapping

    def roles_of(self, sids: list[str]) -> Iterable[str]:
        for s in sids:
            yield from self._mapping.roles_of(s)


class SidExcludeUserProvider:
    """Список SID групп, членам которых запрещён вход"""

    def __init__(self, mapping: RoleExcludeConfig):
        self._mapping = mapping

    def exclude_of(self, sids: list[str]) -> Iterable[bool]:
        for s in sids:
            yield from self._mapping.exclude_of(s)
