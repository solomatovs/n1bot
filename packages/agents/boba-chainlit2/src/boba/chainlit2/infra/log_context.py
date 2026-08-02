"""Пользователь сессии в каждой строке лога без передачи его в вызовы.

Подстановкой занимается фабрика LogRecord: она срабатывает на любой записи,
включая логи chainlit, uvicorn и сторонних библиотек.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from boba.chainlit2.infra.session import current_user_label

__all__ = ["UserLogContext"]


class UserLogContext:
    """Добавляет полю `user` значение из сессии chainlit."""

    ATTRIBUTE: ClassVar[str] = "user"
    UNKNOWN: ClassVar[str] = "-"

    _installed: ClassVar[bool] = False
    _previous: ClassVar[Any] = None

    @classmethod
    def install(cls) -> None:
        """Идемпотентно: повторный вызов не наслаивает фабрики."""
        if cls._installed:
            return
        cls._previous = logging.getLogRecordFactory()
        logging.setLogRecordFactory(cls._make_record)
        cls._installed = True

    @classmethod
    def _make_record(cls, *args: Any, **kwargs: Any) -> logging.LogRecord:
        record = cls._previous(*args, **kwargs)
        setattr(record, cls.ATTRIBUTE, cls._label())
        return record

    @classmethod
    def _label(cls) -> str:
        # логирование не имеет права падать из-за отсутствия контекста
        try:
            label = current_user_label()
        except Exception:
            return cls.UNKNOWN
        if not label:
            return cls.UNKNOWN
        return label
