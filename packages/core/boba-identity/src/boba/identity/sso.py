"""SSO-вход: допуск принципала (порт).

Ошибки: своих не выпускает; AuthorizationError — отказ в допуске у реализации.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from typing import Protocol

__all__ = ["SsoAdmission"]


class SsoAdmission(Protocol):
    """Допуск принципала ко входу: роли этого входа либо отказ."""

    @abstractmethod
    async def roles_of(self, principal: str, group_sids: Sequence[str]) -> list[str]:
        """Роли принципала; AuthorizationError — вход запрещён."""
