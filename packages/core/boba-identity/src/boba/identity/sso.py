"""SSO-вход: допуск принципала (порт) и формат заголовка со списком SID.

Ошибки: своих не выпускает; AuthorizationError — отказ в допуске у реализации.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable, Iterator, Sequence
from typing import Protocol

__all__ = ["SidsHeader", "SsoAdmission"]


class SsoAdmission(Protocol):
    """Допуск принципала ко входу: роли этого входа либо отказ."""

    @abstractmethod
    async def roles_of(self, principal: str, group_sids: Sequence[str]) -> list[str]:
        """Роли принципала; AuthorizationError — вход запрещён."""


class SidsHeader:
    "Формат заголовка со списком SID: сериализация и разбор в одном месте."

    @staticmethod
    def render(sids: Iterable[str]) -> str:
        return ",".join(sids)

    @staticmethod
    def parse(raw: str) -> list[str]:
        return list(SidsHeader._parts(raw))

    @staticmethod
    def _parts(raw: str) -> Iterator[str]:
        for part in raw.split(","):
            if not part:
                continue
            yield part
