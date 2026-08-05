"""Точка входа payload'а: запрос со stdin, кадры и трейлер в stdout.

Логи инструмента едут кадрами в stderr: stdout занят данными, а stderr на
исход операции не влияет — его читает только релей хоста и сливает в общий
журнал приложения. Инструменту достаточно обычного logging.getLogger.

Ошибки делятся на два класса. Ожидаемые — объявленные в PayloadOps.EXPECTED
типы и PayloadError — уходят кадром `sandbox-error:` с готовым для пользователя
текстом; трейсбек в этом случае не печатается. Всё остальное не ловится и
падает штатным трейсбеком интерпретатора: неизвестную ошибку прятать нельзя.
Код возврата в обоих случаях ненулевой — отказ остаётся отказом.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from abc import abstractmethod
from collections.abc import Callable, Coroutine, Mapping
from typing import Any, ClassVar, Protocol, TypeAlias

from pydantic import ValidationError

from boba.toolkit.launcher import LaunchPayload

__all__ = [
    "ChunkEmitter",
    "PayloadEntry",
    "PayloadError",
    "PayloadLogging",
    "PayloadOps",
]

ChunkEmitter: TypeAlias = Callable[[str], None]


class PayloadLogFormatter(logging.Formatter):
    """Запись логера -> кадр `sandbox-log:` одной строкой."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return LaunchPayload.encode_log(record.levelname, record.name, message)


class PayloadLogging:
    """Логер payload'а: кадры в stderr вместо свободного текста.

    Уровень не настраивается здесь: его вычисляет хост из своей секции logger
    и передаёт переменной окружения — так у настройки остаётся один источник.
    """

    LEVEL_ENV: ClassVar[str] = "BOBA_LOG_LEVEL"
    """Канал доставки уровня от хоста; в конфиге такой ручки нет."""

    FALLBACK_LEVEL: ClassVar[str] = "INFO"
    """Только для запуска payload'а руками, без хоста."""

    @classmethod
    def setup(cls) -> None:
        """Ставится один раз на процесс; уровень приходит от хоста."""
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(PayloadLogFormatter())
        logging.basicConfig(level=cls.level(), handlers=[handler], force=True)

    @classmethod
    def level(cls) -> int:
        raw = os.environ.get(cls.LEVEL_ENV, cls.FALLBACK_LEVEL).upper()
        resolved = logging.getLevelName(raw)
        if isinstance(resolved, int):
            return resolved
        return logging.INFO


class PayloadError(Exception):
    """Ожидаемая ошибка операции с готовой формулировкой для пользователя."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class PayloadOps(Protocol):
    """Контракт payload'а: операции плюс объявленные ожидаемые ошибки.

    EXPECTED перечисляет типы, которые для этого инструмента являются штатным
    отказом: их текст едет пользователю без трейсбека. Пустая мапа означает,
    что ожидаемых ошибок у инструмента нет.
    """

    EXPECTED: ClassVar[Mapping[type[Exception], str]]

    @classmethod
    @abstractmethod
    def dispatch(
        cls,
        request: dict[str, Any],
        emit: ChunkEmitter,
    ) -> Coroutine[Any, Any, dict[str, Any]]:
        """Выполнить операцию запроса; данные уходят через emit."""
        ...


class PayloadEntry:
    """Разбор запроса, печать кадров и трейлера; операцию выбирает инструмент."""

    CHUNK_CHARS: ClassVar[int] = 64 * 1024

    FAILURE_CODE: ClassVar[int] = 1

    BUILTIN_EXPECTED: ClassVar[Mapping[type[Exception], str]] = {
        ValidationError: "invalid_request",
    }
    """Ожидаемое для любого payload'а: запрос не по контракту."""

    @staticmethod
    def emit_text(emit: ChunkEmitter, text: str) -> None:
        """Материализованный текст уходит кадрами ограниченного размера."""
        for start in range(0, len(text), PayloadEntry.CHUNK_CHARS):
            emit(text[start : start + PayloadEntry.CHUNK_CHARS])

    @staticmethod
    def main(ops: type[PayloadOps]) -> int:
        PayloadLogging.setup()
        request = json.loads(sys.stdin.read())
        try:
            trailer = asyncio.run(ops.dispatch(request, PayloadEntry.emit))
        except PayloadError as e:
            PayloadEntry._write_error(e.kind, e.message)
            return PayloadEntry.FAILURE_CODE
        except Exception as e:
            kind = PayloadEntry._expected_kind(ops, e)
            if kind is None:
                raise
            PayloadEntry._write_error(kind, PayloadEntry._reason(e))
            return PayloadEntry.FAILURE_CODE

        PayloadEntry._write_trailer(trailer)
        return 0

    @staticmethod
    def emit(chunk: str) -> None:
        """Кадр уходит сразу: flush отдаёт данные хосту, не дожидаясь конца."""
        sys.stdout.write(LaunchPayload.encode_chunk(chunk))
        sys.stdout.write("\n")
        sys.stdout.flush()

    @staticmethod
    def _expected_kind(ops: type[PayloadOps], error: Exception) -> str | None:
        """Kind объявленного типа; учитываются и подклассы объявленного."""
        for source in (ops.EXPECTED, PayloadEntry.BUILTIN_EXPECTED):
            for declared, kind in source.items():
                if isinstance(error, declared):
                    return kind
        return None

    @staticmethod
    def _reason(error: Exception) -> str:
        """Текст исключения; у части библиотечных ошибок он пуст — тогда имя типа."""
        text = str(error).strip()
        if text:
            return f"{type(error).__name__}: {text}"
        return type(error).__name__

    @staticmethod
    def _write_error(kind: str, message: str) -> None:
        """Кадр ошибки хосту; в журнал тот же факт уходит обычным логом."""
        if not message.strip():
            message = kind
        sys.stdout.write(LaunchPayload.encode_error(kind, message))
        sys.stdout.write("\n")
        sys.stdout.flush()
        logging.getLogger(__name__).error("%s: %s", kind, message)

    @staticmethod
    def _write_trailer(trailer: dict[str, Any]) -> None:
        body = json.dumps(trailer, ensure_ascii=False)
        sys.stdout.write(LaunchPayload.MARKER)
        sys.stdout.write(body)
        sys.stdout.write("\n")
        sys.stdout.flush()
