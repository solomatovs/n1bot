"""Соединения субъекта по имени: выбор строки под запрос вызова.

Имя, выданное субъекту дважды внутри вида (лично и через роль, две роли),
числится неоднозначным: выбирать наугад нельзя, и такое имя не резолвится.
Дубли считает SubjectGrantsQuery из boba.access, сюда они приходят признаком строки.

Ошибки:
AmbiguousConnectionError — запрошенное имя выдано субъекту дважды.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from pydantic import BaseModel, ConfigDict

from boba.connections.profile import ConnectionProfileBase, GrantedConnection

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
    def of(cls, granted: Iterable[GrantedConnection]) -> ConnectionWhitelist:
        profiles: dict[str, ConnectionProfileBase] = {}
        ambiguous: set[str] = set()
        for item in granted:
            if item.ambiguous:
                ambiguous.add(item.row.name)
                continue

            row = item.row
            profiles[row.name] = row.profile.identified(row.id, row.name)

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
