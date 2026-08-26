"""Whitelist инструмента из соединений субъекта: имя -> профиль.

Все инструменты выбирают соединение tool-arg'ом connection_name; имя,
встреченное у субъекта дважды (лично и через роль, две роли), числится
неоднозначным и в whitelist не попадает.

Ошибки:
AmbiguousConnectionError — запрошенное имя выдано субъекту дважды.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from boba.connections.profile import ConnectionProfile, StoredConnection

__all__ = [
    "AmbiguousConnectionError",
    "ConnectionKeying",
    "ConnectionWhitelist",
    "Picked",
]


class AmbiguousConnectionError(LookupError):
    """Запрошенное имя выдано субъекту несколько раз; выбирать наугад нельзя."""


class ConnectionKeying(StrEnum):
    """Чем инструмент адресует соединение; значение — имя tool-arg."""

    NAME = "connection_name"

    def key_of(self, row: StoredConnection) -> str:
        return row.name

    def requested(self, kwargs: Mapping[str, object]) -> str:
        """Ключ, который запросил вызов; пустая строка — аргумента нет."""
        value = kwargs.get(self.value)
        if not isinstance(value, str):
            return ""

        return value


class Picked(BaseModel):
    """Строка, выбранная под запрос вызова."""

    model_config = ConfigDict(frozen=True)

    key: str
    profile: ConnectionProfile


class ConnectionWhitelist(BaseModel):
    """Профили по имени плюс имена-дубли."""

    model_config = ConfigDict(frozen=True)

    keying: ConnectionKeying
    profiles: Mapping[str, ConnectionProfile]
    ambiguous: frozenset[str]

    @classmethod
    def of(
        cls, rows: Iterable[StoredConnection], keying: ConnectionKeying
    ) -> ConnectionWhitelist:
        by_key: dict[str, list[StoredConnection]] = {}
        for row in rows:
            by_key.setdefault(keying.key_of(row), []).append(row)

        profiles: dict[str, ConnectionProfile] = {}
        for key, row in cls._unique(by_key):
            profiles[key] = row.profile

        ambiguous: list[str] = []
        for key, group in by_key.items():
            if len(group) > 1:
                ambiguous.append(key)

        return cls(keying=keying, profiles=profiles, ambiguous=frozenset(ambiguous))

    def pick(self, requested: str) -> Picked | None:
        """Строка под запрос; None — такого имени у субъекта нет.

        Ошибки:
        AmbiguousConnectionError — имя выдано субъекту дважды.
        """
        if requested in self.ambiguous:
            msg = f"connection {requested!r} is granted more than once"
            raise AmbiguousConnectionError(msg)

        profile = self.profiles.get(requested)
        if profile is None:
            return None

        return Picked(key=requested, profile=profile)

    @staticmethod
    def _unique(
        by_key: Mapping[str, Sequence[StoredConnection]],
    ) -> Iterator[tuple[str, StoredConnection]]:
        for key, group in by_key.items():
            if len(group) != 1:
                continue

            yield key, group[0]
