from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_NOISY_LOGGERS = (
    "watchdog",
    "httpx",
    "httpcore",
    "engineio.server",
)

_NO_ID = "-"
_ID_WIDTH = 8

_REQUEST_ID: ContextVar[str] = ContextVar("boba_request_id", default=_NO_ID)
_WORKSPACE_ID: ContextVar[str] = ContextVar("boba_workspace_id", default=_NO_ID)


def _short(value: str) -> str:
    if not value or value == _NO_ID:
        return _NO_ID
    return value[:_ID_WIDTH]


@contextmanager
def log_context(
    *, request_id: str = _NO_ID, workspace_id: str = _NO_ID
) -> Iterator[None]:
    """Привязать request_id / workspace_id к текущему ContextVar-стеку.

    Вложенные вызовы работают корректно благодаря :meth:`ContextVar.reset` —
    при выходе из блока восстановятся предыдущие значения.
    """
    tok_req = _REQUEST_ID.set(request_id)
    tok_ws = _WORKSPACE_ID.set(workspace_id)
    try:
        yield
    finally:
        _REQUEST_ID.reset(tok_req)
        _WORKSPACE_ID.reset(tok_ws)


class _ContextFilter(logging.Filter):
    """Подмешивает request_id / workspace_id в каждую запись лога."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _short(_REQUEST_ID.get())
        record.workspace_id = _short(_WORKSPACE_ID.get())
        return True


def configure_logging(
    log_level: str = "INFO", log_file: str | None = None
) -> None:
    """
    Процессный bootstrap логов: handler, формат, фильтр контекста.

    Если ``log_file`` задан — пишем в файл (append), родительская
    директория создаётся при необходимости. Если ``None`` — в stdout.
    Идемпотентно: существующие handler'ы root-логгера сбрасываются, что
    позволяет безопасно звать функцию повторно (например, в тестах при
    пересоздании :class:`AgentHarness`).
    """
    level = _LOG_LEVELS.get(log_level.upper(), logging.INFO)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(
            path, mode="a", encoding="utf-8"
        )
    else:
        handler = logging.StreamHandler(sys.stdout)

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s "
            "[req=%(request_id)s ws=%(workspace_id)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    handler.addFilter(_ContextFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    root.setLevel(level)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
