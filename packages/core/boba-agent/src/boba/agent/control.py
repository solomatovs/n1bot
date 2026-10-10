"""Вопросы агента наружу и ответы на них: `can_use_tool`.

Агент не ждёт ответа на вопрос сам по себе: он отдаёт вопрос событием
`ControlRequestEvent`, а запускающая программа когда-нибудь зовёт
`Agent.answer` с тем же `request_id` (план, раздел 5.8). Реестр
ControlQuestions держит ожидания по `request_id`: вопрос, снятый отменой
хода, уходит событием `ControlCancelEvent`, поздний или повторный ответ
отбрасывается с записью в лог. Тем же вопросом задаётся и собственный
инструмент агента «вопрос пользователю»: его ввод едет полем `input`, ответ
пользователя возвращается в `updated_input`.

Ошибки:
QuestionWithdrawnError — вопрос снят отменой хода, пока агент ждал ответа.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from boba.agent.ids import IdMint
from boba.agent.records import PermissionRule
from boba.cancellation import RunCancellation

logger = logging.getLogger(__name__)

__all__ = [
    "AllowAnswer",
    "CanUseToolRequest",
    "ControlQuestions",
    "ControlSubtype",
    "DenyAnswer",
    "PermissionAnswer",
    "PermissionBehavior",
    "QuestionWithdrawnError",
]


class QuestionWithdrawnError(Exception):
    """Вопрос снят: ход отменён, пока ответа не было."""


class ControlSubtype(StrEnum):
    """Виды вопросов наружу первой версии."""

    CAN_USE_TOOL = "can_use_tool"


class PermissionBehavior(StrEnum):
    """Что ответила запускающая программа на вопрос о вызове."""

    ALLOW = "allow"
    DENY = "deny"


class ControlModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class CanUseToolRequest(ControlModel):
    """Вопрос «можно ли вызвать инструмент»: имя, ввод вызова и готовые
    правила, которыми можно ответить «разрешить до конца сессии»."""

    subtype: Literal[ControlSubtype.CAN_USE_TOOL] = ControlSubtype.CAN_USE_TOOL
    tool_name: str
    input: Mapping[str, Any]
    tool_use_id: str
    permission_suggestions: Sequence[PermissionRule] = ()


class AllowAnswer(ControlModel):
    """Разрешить: с заменённым вводом и правилами на остаток сессии."""

    behavior: Literal[PermissionBehavior.ALLOW] = PermissionBehavior.ALLOW
    updated_input: Mapping[str, Any] | None = None
    updated_permissions: Sequence[PermissionRule] = ()


class DenyAnswer(ControlModel):
    """Отказать: с пояснением пользователя или без него."""

    behavior: Literal[PermissionBehavior.DENY] = PermissionBehavior.DENY
    message: str = ""


PermissionAnswer: TypeAlias = AllowAnswer | DenyAnswer


class PendingQuestion(BaseModel):
    """Открытый вопрос: его запрос и ожидание ответа."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    request: CanUseToolRequest
    answer: asyncio.Future[PermissionAnswer] = Field(repr=False)


class ControlQuestions:
    """Реестр открытых вопросов наружу по `request_id`.

    Создаёт агент на сессию; цикл хода задаёт вопросы через `ask`, вход
    процесса отвечает через `answer`. Событие с вопросом и событие снятия
    отдаёт тот, кто ждёт: `ask` возвращает запрос с идентификатором, а
    снятие сообщает исключением; так реестр не знает про порт событий.
    """

    def __init__(self, ids: IdMint) -> None:
        self._ids = ids
        self._pending: dict[str, PendingQuestion] = {}

    def open(self, request: CanUseToolRequest) -> str:
        """Регистрирует вопрос и выдаёт его `request_id`."""
        request_id = self._ids.request_id()
        answer: asyncio.Future[PermissionAnswer] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = PendingQuestion(request=request, answer=answer)

        return request_id

    async def wait(
        self, request_id: str, cancellation: RunCancellation
    ) -> PermissionAnswer:
        """Ждёт ответа на открытый вопрос; отмена хода снимает вопрос.

        Ошибки:
        QuestionWithdrawnError — ход отменён, ответа не будет.
        """
        pending = self._pending[request_id]
        try:
            with cancellation.abort_with(self._withdrawer(pending)):
                return await pending.answer
        except asyncio.CancelledError as exc:
            if not pending.answer.cancelled():
                raise

            msg = (
                f"The question {request_id} about '{pending.request.tool_name}' was "
                "withdrawn: the turn was cancelled before an answer arrived."
            )
            raise QuestionWithdrawnError(msg) from exc
        finally:
            self._pending.pop(request_id, None)

    def _withdrawer(self, pending: PendingQuestion) -> Callable[[], None]:
        def withdraw() -> None:
            pending.answer.cancel()

        return withdraw

    def answer(self, request_id: str, answer: PermissionAnswer) -> bool:
        """Отдаёт ответ ожидающему; False — вопроса нет или он уже отвечен."""
        pending = self._pending.get(request_id)
        if pending is None:
            logger.warning(
                "control: an answer for an unknown or already answered question %s "
                "was dropped",
                request_id,
            )
            return False

        if pending.answer.done():
            logger.warning(
                "control: a second answer for question %s was dropped", request_id
            )
            return False

        pending.answer.set_result(answer)

        return True

    def pending(self) -> Sequence[str]:
        return list(self._pending)
