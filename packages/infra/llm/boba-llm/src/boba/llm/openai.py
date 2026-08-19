"""Транспорт openai-совместимого провайдера: конфиг endpoint'а и httpx-клиент.

Ошибки: своих не выпускает; ошибки httpx уходят вызывающему как есть.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from boba.llm.dump import DumpingTransport

__all__ = [
    "OpenAiConfig",
    "OpenAiDumpConfig",
    "OpenAiHttp",
]


class OpenAiDumpConfig(BaseModel):
    """Дамп HTTP-обмена с провайдером: флаг и каталог файлов."""

    model_config = ConfigDict(extra="ignore")

    enable: bool = Field(
        default=False,
        description="Писать HTTP-обмен с провайдером в path.",
    )

    path: str = Field(
        default="",
        description="Каталог дампов; обязателен при enable = true.",
    )

    @model_validator(mode="after")
    def _validate_path(self) -> Self:
        if not self.enable:
            return self

        if not self.path:
            msg = "openai.dump: enable = true требует path"
            raise ValueError(msg)

        return self


class OpenAiConfig(BaseModel):
    """Транспорт openai-совместимого провайдера: endpoint + httpx-тюнинг."""

    model_config = ConfigDict(extra="ignore")

    base_url: str = Field(description="Endpoint openai-совместимого API.")

    api_key: str = Field(description="Ключ API провайдера.")

    ssl_verify: bool = Field(
        default=True,
        description="Проверять TLS-сертификат сервера.",
    )

    dump: OpenAiDumpConfig = Field(
        default_factory=OpenAiDumpConfig,
        description="Дамп HTTP-обмена с провайдером.",
    )

    trust_env: bool = Field(
        default=True,
        description=(
            "Брать прокси и CA из окружения (HTTPS_PROXY, SSL_CERT_FILE); "
            "false — окружение игнорируется целиком."
        ),
    )

    proxy: str = Field(
        default="",
        description="Прокси для запросов к провайдеру; пусто — напрямую.",
    )

    http2: bool = Field(
        default=False,
        description="Разрешить HTTP/2 к провайдеру.",
    )

    stream_chunk_timeout: float = Field(
        default=600,
        ge=0,
        description=(
            "Пауза между чанками контента в стриме, секунды: считает разрывы "
            "между разобранными чанками, keepalive её не сбрасывает. "
            "0 — без потолка."
        ),
    )

    max_retries: int = Field(
        default=2,
        ge=0,
        description=(
            "Повторы запроса клиентом openai при 429/5xx и таймауте; "
            "каждый повтор — новая полная попытка."
        ),
    )

    connect_timeout: float = Field(
        default=5,
        description="установка TCP-соединения с хостом (включая TLS handshake)",
    )

    read_timeout: float = Field(
        default=600,
        description=(
            "ожидание очередных байт от сервера; при stream=True — пауза "
            "между байтами, а не между чанками контента"
        ),
    )

    write_timeout: float = Field(
        default=100,
        description="отправка тела запроса на сервер",
    )

    pool_timeout: float = Field(
        default=5,
        description=("ожидание свободного соединения из пула httpx (когда все заняты)"),
    )

    max_connections: int = Field(
        default=50,
        description="",
    )

    max_keepalive_connections: int = Field(
        default=10,
        description="",
    )

    keepalive_expiry: float = Field(
        default=5,
        description="",
    )

    retries: int = Field(
        default=3,
        description="Число повторов установления соединения в httpx-транспорте.",
    )

    tcp_keepalive: bool = Field(
        default=True,
        description=(
            "TCP keepalive (SO_KEEPALIVE): защита от молчаливого разрыва "
            "простаивающего соединения файрволом (обычно режут после 5-10 минут "
            "тишины)."
        ),
    )

    tcp_keepidle: int = Field(
        default=60,
        description=(
            "TCP_KEEPIDLE: секунд простоя, после которых ядро шлёт "
            "keepalive-пробу. Без явного значения берётся sysctl "
            "tcp_keepalive_time — обычно 7200 (2 часа простоя!)."
        ),
    )

    tcp_keepintvl: int = Field(
        default=10,
        description="TCP_KEEPINTVL: интервал между повторными пробами (сек).",
    )

    tcp_keepcnt: int = Field(
        default=10,
        description=(
            "TCP_KEEPCNT: число безответных проб, после которых соединение "
            "считается мёртвым; следующая работа с сокетом даст "
            "ECONNABORTED/ETIMEDOUT."
        ),
    )

    tcp_user_timeout: int = Field(
        default=0,
        ge=0,
        description=(
            "TCP_USER_TIMEOUT: сколько миллисекунд ядро ждёт подтверждения "
            "уже отправленных данных, прежде чем оборвать соединение; в "
            "отличие от keepalive работает и на активном обмене. 0 — не задавать."
        ),
    )


class OpenAiHttp:
    """Сборка httpx-клиента и таймаутов из OpenAiConfig — одна на всех потребителей."""

    @staticmethod
    def timeout(c: OpenAiConfig) -> httpx.Timeout:
        return httpx.Timeout(
            connect=c.connect_timeout,
            read=c.read_timeout,
            write=c.write_timeout,
            pool=c.pool_timeout,
        )

    @classmethod
    def client(
        cls,
        c: OpenAiConfig,
        dump_file: Callable[[httpx.Request], str] | None = None,
    ) -> httpx.AsyncClient:
        """Клиент с транспортом по конфигу; при dump.enable обмен пишется в файлы."""
        if c.dump.enable:
            transport: httpx.AsyncHTTPTransport = cls._dump_transport(c, dump_file)
        else:
            transport = httpx.AsyncHTTPTransport(**cls._transport_options(c))

        return httpx.AsyncClient(
            timeout=cls.timeout(c),
            transport=transport,
        )

    @classmethod
    def _dump_transport(
        cls,
        c: OpenAiConfig,
        dump_file: Callable[[httpx.Request], str] | None,
    ) -> DumpingTransport:
        label = dump_file
        if label is None:
            label = cls._host_dump_file

        return DumpingTransport(
            dump_dir=Path(c.dump.path),
            dump_file=label,
            **cls._transport_options(c),
        )

    @staticmethod
    def _host_dump_file(request: httpx.Request) -> str:
        return f"{request.url.host}.log"

    @classmethod
    def _transport_options(cls, c: OpenAiConfig) -> dict[str, Any]:
        limits = httpx.Limits(
            max_connections=c.max_connections,
            max_keepalive_connections=c.max_keepalive_connections,
            keepalive_expiry=c.keepalive_expiry,
        )
        verify = httpx.create_ssl_context(
            verify=c.ssl_verify, cert=None, trust_env=c.trust_env
        )

        proxy = None
        if c.proxy:
            proxy = c.proxy

        return {
            "http2": c.http2,
            "verify": verify,
            "limits": limits,
            "proxy": proxy,
            "trust_env": c.trust_env,
            "retries": c.retries,
            "socket_options": cls._socket_options(c),
        }

    @staticmethod
    def _socket_options(c: OpenAiConfig) -> list[tuple[int, int, int]]:
        """Опции сокета: keepalive против молчаливого разрыва, user timeout —
        против соединения, которое приняло данные и замолчало."""
        options: list[tuple[int, int, int]] = [
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, int(c.tcp_keepalive)),
        ]

        if c.tcp_keepalive:
            options += [
                (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, c.tcp_keepidle),
                (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, c.tcp_keepintvl),
                (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, c.tcp_keepcnt),
            ]

        if c.tcp_user_timeout:
            options.append(
                (socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, c.tcp_user_timeout)
            )

        return options
