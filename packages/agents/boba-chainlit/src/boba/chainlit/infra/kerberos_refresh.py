"""Сигнал обновления SPNEGO в сокет сессии чата: реализация RefreshSignal."""

from __future__ import annotations

import logging
from typing import ClassVar

from boba.chainlit.domain.context import ChatCallContext
from boba.connection_broker.user_connections import RefreshSignal
from boba.identity.context import CallContext

__all__ = ["ChatRefreshSignal"]

logger = logging.getLogger(__name__)


class ChatRefreshSignal(RefreshSignal):
    """Просьба к фронту молча пройти SPNEGO ещё раз: сигнал в сокет сессии.

    Адрес обмена знает сам скрипт страницы: сервер сообщает только повод.
    """

    TYPE: ClassVar[str] = "boba:kerberos-refresh"
    EVENT: ClassVar[str] = "window_message"

    async def send(self) -> bool:
        payload = {"type": self.TYPE}
        context = CallContext.current()
        if not isinstance(context, ChatCallContext):
            return False

        try:
            return await context.surface.emit(self.EVENT, payload)
        except Exception:
            logger.warning("kerberos refresh signal failed", exc_info=True)
            return False
