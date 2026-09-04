"""Соединения субъекта по имени: выбор строки под запрос вызова.

Имя, встреченное у субъекта дважды (лично и через роль, две роли), числится
неоднозначным: выбирать наугад нельзя, и такое имя не резолвится.

Ошибки:
AmbiguousConnectionError — запрошенное имя выдано субъекту дважды.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from boba.connections.profile import ConnectionProfileBase, StoredConnection

__all__ = [
    "AmbiguousConnectionError",
    "ConnectionWhitelist",
    "Picked",
]


class AmbiguousConnectionError(LookupError):
    """Запрошенное имя выдано субъекту несколько раз; выбирать наугад нельзя."""


class Picked(BaseModel):
    """Строка, выбранная под запрос вызова."""

    model_config = ConfigDict(frozen=True)

    name: str
    profile: ConnectionProfileBase


class ConnectionWhitelist(BaseModel):
    """Профили субъекта по имени плюс имена-дубли."""

    model_config = ConfigDict(frozen=True)

    profiles: Mapping[str, ConnectionProfileBase]
    ambiguous: frozenset[str]

    @classmethod
    def of(cls, rows: Iterable[StoredConnection]) -> ConnectionWhitelist:
        by_name: dict[str, list[StoredConnection]] = {}
        for row in rows:
            by_name.setdefault(row.name, []).append(row)

        profiles: dict[str, ConnectionProfileBase] = {}
        for name, row in cls._unique(by_name):
            profiles[name] = row.profile

        ambiguous: list[str] = []
        for name, group in by_name.items():
            if len(group) > 1:
                ambiguous.append(name)

        return cls(profiles=profiles, ambiguous=frozenset(ambiguous))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.profiles))

    def pick(self, requested: str) -> Picked | None:
        """Строка под запрос; None — такого имени у субъекта нет.

        Ошибки:
        AmbiguousConnectionError — имя выдано субъекту дважды.
        """
        if requested in self.ambiguous:
            msg = (
                f"connection {requested!r} is granted to the subject more than "
                "once, the name is ambiguous"
            )
            raise AmbiguousConnectionError(msg)

        profile = self.profiles.get(requested)
        if profile is None:
            return None

        return Picked(name=requested, profile=profile)

    @staticmethod
    def _unique(
        by_name: Mapping[str, Sequence[StoredConnection]],
    ) -> Iterator[tuple[str, StoredConnection]]:
        for name, group in by_name.items():
            if len(group) != 1:
                continue

            yield name, group[0]
