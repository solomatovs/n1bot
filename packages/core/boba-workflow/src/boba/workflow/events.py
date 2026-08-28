"""Снимок запуска для получателей: id, статус и состояние целиком.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from boba.workflow.graph import RunState, RunStatus

__all__ = ["RunSnapshot"]


class RunSnapshot(BaseModel):
    """Снимок запуска для страницы и слушателей: id, статус и состояние графа."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    status: RunStatus
    state: RunState

    @property
    def finished(self) -> bool:
        return self.status.terminal
