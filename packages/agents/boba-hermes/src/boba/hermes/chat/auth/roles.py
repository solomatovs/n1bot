from collections.abc import Iterable

from pydantic import RootModel


class RoleMappingConfig(RootModel[dict[str, list[str]]]):
    """Статический мапер пользователь - список ролей"""

    def roles_of(self, key: str) -> list[str]:
        return self.root.get(key, [])


class RoleExcludeConfig(RootModel[list[str]]):
    """Статический список исключённых пользователей/ролей"""

    def exclude_of(self, key: str) -> Iterable[bool]:
        for x in self.root:
            yield x == key


class StaticRolesProvider:
    """Статический провайдер пользователь - список ролей"""

    def __init__(self, mapping: RoleMappingConfig):
        self._mapping = mapping

    def roles_of(self, username: str) -> Iterable[str]:
        yield from self._mapping.roles_of(username)


class StaticExcludeProvider:
    """Статический список пользователей, которым запрещён вход"""

    def __init__(self, mapping: RoleExcludeConfig):
        self._mapping = mapping

    def exclude_of(self, username: str) -> Iterable[bool]:
        yield from self._mapping.exclude_of(username)
