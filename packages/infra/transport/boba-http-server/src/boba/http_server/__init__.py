"""Настройки, проверка готовности и жизненный цикл HTTP-сервера.

Ошибки:
HealthCheckError — служба не ответила на пробу готовности успехом.
ServerStartError — сервер не смог запустить приложение.
ProcessSignalError — подписка или обработка сигналов процесса не выполнены;
    перехватывается через boba.cancellation, включая сбой действий остановки.
"""

from __future__ import annotations

import signal
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from typing import Any, ClassVar

import uvicorn
from pydantic import (
    ConfigDict,
    Field,
)
from uvicorn.lifespan.on import LifespanOn

from boba.cancellation import ProcessStop
from boba.identity.service import PublicAddress
from boba.tool_io.closing import ProcessClosers


class ServerConfig(PublicAddress):
    """Общие параметры HTTP-сервера приложения."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(gt=0, lt=65536)
    shutdown_timeout_sec: int = Field(default=10, gt=0)


class ProcessLogging:
    """Логирование процесса по умолчанию: приложение в stderr, access-лог в stdout."""

    def default(self) -> dict[str, Any]:
        return {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "()": "uvicorn.logging.DefaultFormatter",
                    "fmt": "%(levelprefix)s %(message)s",
                    "use_colors": True,
                },
                "access": {
                    "()": "uvicorn.logging.AccessFormatter",
                    "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',  # noqa: E501
                },
            },
            "handlers": {
                "default": {
                    "formatter": "default",
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stderr",
                },
                "access": {
                    "formatter": "access",
                    "class": "logging.StreamHandler",
                    "stream": "ext://sys.stdout",
                },
            },
            "root": {"handlers": ["default"], "level": "INFO"},
            "loggers": {
                "uvicorn": {
                    "handlers": ["default"],
                    "level": "INFO",
                    "propagate": False,
                },
                "uvicorn.error": {"level": "INFO"},
                "uvicorn.access": {
                    "handlers": ["access"],
                    "level": "INFO",
                    "propagate": False,
                },
            },
        }


class HealthCheckError(Exception):
    """Служба не ответила на пробу готовности успехом."""


class HealthProbe:
    """Проба готовности службы: GET на свой порт и префикс по loopback.

    Её зовёт подкоманда `health` программы (HEALTHCHECK образа, проверка
    юнита) над портом и префиксом из своей секции конфига; путь пробы —
    маршрут готовности самой службы.
    """

    LOOPBACK: ClassVar[str] = "127.0.0.1"
    SCHEME: ClassVar[str] = "http"
    TIMEOUT_SEC: ClassVar[float] = 4.0

    def __init__(self, port: int, url_prefix: str, path: str) -> None:
        self._url = urllib.parse.urlunsplit(
            (self.SCHEME, f"{self.LOOPBACK}:{port}", f"{url_prefix}{path}", "", "")
        )

    def check(self) -> str:
        """Строка итога пробы; не 2xx или нет ответа — HealthCheckError."""
        try:
            # схема http и адрес 127.0.0.1 собраны здесь: file:/ сюда не попадает
            with urllib.request.urlopen(self._url, timeout=self.TIMEOUT_SEC) as answer:  # noqa: S310  # nosec B310
                status = answer.status
        except (urllib.error.URLError, OSError) as exc:
            msg = f"health probe GET {self._url}: expected a 2xx answer, got {exc}"
            raise HealthCheckError(msg) from exc

        return f"health probe GET {self._url}: {status}"


class ServerStartError(Exception):
    """Приложение сервера не поднялось: его lifespan сорвался на старте."""


class SignalledServer(uvicorn.Server):
    """HTTP-сервер с единственным владельцем сигналов — ProcessSignals.

    Приложение описывает остановку в ProcessStop; этот адаптер останавливает
    приём запросов Uvicorn. Повторный TERM/INT ускоряет остановку, но lifespan
    приложения всё равно закрывается. Собственная подписка Uvicorn отключена.
    """

    def __init__(self, config: uvicorn.Config, stop: ProcessStop) -> None:
        super().__init__(config)
        self._stop = stop
        self._closers = ProcessClosers()
        for received in stop.handled:
            stop.signals.on(received, self._exit)

    async def serve_until_stopped(self, sockets: Sequence[socket.socket] = ()) -> None:
        """Слушает адрес настроек uvicorn либо готовые сокеты sockets до
        сигнала остановки и возвращается штатно.

        Ошибки:
        ServerStartError — приложение не поднялось.
        ProcessSignalError — ошибка подписок или действий сигналов; тип
            экспортируется boba.cancellation, причины действий сохранены.
        """
        listeners: list[socket.socket] | None = None
        if sockets:
            listeners = list(sockets)

        async with self._stop.signals:
            try:
                await self.serve(listeners)
            finally:
                self._stop.signals.cancel_pending()
                await self._closers.close_all()

        if not self.started:
            msg = (
                f"server on {self.config.host}:{self.config.port}: the application "
                "startup failed, the cause is logged above by the lifespan; "
                "expected a started server, exiting with a failure"
            )
            raise ServerStartError(msg)

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        await super().shutdown(sockets)

        if not isinstance(self.lifespan, LifespanOn):
            return

        if self.lifespan.shutdown_event.is_set():
            return

        await self.lifespan.shutdown()

    @contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        """Подписками владеет ProcessSignals; Uvicorn не меняет их и не повторяет."""
        yield

    def _exit(self, received: signal.Signals) -> None:
        if self.should_exit:
            self.force_exit = True
        self.should_exit = True
