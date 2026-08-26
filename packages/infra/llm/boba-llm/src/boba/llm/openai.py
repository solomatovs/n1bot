"""httpx-клиент openai-совместимого провайдера по конфигу boba.chat.openai.

Ошибки: своих не выпускает; ошибки httpx уходят вызывающему как есть.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from boba.chat.openai import OpenAiConfig
from boba.llm.dump import DumpingTransport

__all__ = ["OpenAiHttp"]


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
