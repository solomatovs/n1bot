"""Делегированный билет входа как значение: содержимое ccache и срок."""

from __future__ import annotations

import time

from pydantic import BaseModel, ConfigDict, Field

from boba.krb.config import DelegationMode

__all__ = ["SignInTicket"]


class SignInTicket(BaseModel):
    """Делегированные креды одного SSO-входа: содержимое FILE-ccache и срок.

    Constrained — evidence-тикет пользователя и TGT сервиса, forwarded — TGT
    пользователя. Живёт в JWT сессии запечатанным; процесс ничего не хранит.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: str = Field(min_length=1)
    mode: DelegationMode
    ccache: bytes = Field(min_length=1)
    expires_at: int = Field(gt=0)
    """Конец делегированных кредов, unix-секунды."""

    def lifetime(self) -> int:
        """Остаток кредов, сек; 0 — истекли."""
        remaining = self.expires_at - int(time.time())
        if remaining < 0:
            return 0

        return remaining
