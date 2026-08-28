"""Хранилище тел, которые не лезут в сообщение шины; сообщение несёт ссылку
PayloadRef, тела живут до purge области.

Ошибки:
PayloadMissingError — тела по ссылке нет: область уже убрана или ссылка чужая.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from boba.identity.context import Scope

__all__ = ["MemoryPayloadStore", "PayloadMissingError", "PayloadRef", "PayloadStore"]


class PayloadMissingError(Exception):
    """Тела по ссылке нет: область уже убрана или ссылка из другого хранилища."""


class PayloadRef(BaseModel):
    """Ссылка на тело в хранилище: область, которой оно принадлежит, и id внутри неё."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: Scope
    id: str = Field(min_length=1)


class PayloadStore(Protocol):
    """Порт хранилища тел: кладёт тело в область, отдаёт по ссылке и убирает всё по
    завершении области.
    """

    @abstractmethod
    async def put(self, scope: Scope, payload: object) -> PayloadRef:
        """Кладёт тело в область и возвращает ссылку, которую понесёт сообщение."""

    @abstractmethod
    async def get(self, ref: PayloadRef) -> object:
        """Возвращает тело по ссылке; если его нет — PayloadMissingError."""

    @abstractmethod
    async def purge(self, scope: Scope) -> int:
        """Убирает все тела области и возвращает их число."""


class MemoryPayloadStore(PayloadStore):
    """Хранилище в памяти процесса: годится, пока отправитель и получатель — один
    процесс.
    """

    def __init__(self) -> None:
        self._bodies: dict[Scope, dict[str, object]] = {}

    async def put(self, scope: Scope, payload: object) -> PayloadRef:
        ref = PayloadRef(scope=scope, id=uuid4().hex)
        self._bodies.setdefault(scope, {})[ref.id] = payload
        return ref

    async def get(self, ref: PayloadRef) -> object:
        bodies = self._bodies.get(ref.scope)
        if bodies is None:
            msg = f"payload {ref.id} of {ref.scope.render()} is gone"
            raise PayloadMissingError(msg)

        if ref.id not in bodies:
            msg = f"payload {ref.id} of {ref.scope.render()} is unknown"
            raise PayloadMissingError(msg)

        return bodies[ref.id]

    async def purge(self, scope: Scope) -> int:
        return len(self._bodies.pop(scope, {}))
