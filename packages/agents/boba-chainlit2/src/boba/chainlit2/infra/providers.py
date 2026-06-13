import re
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

import httpx
from httpx import AsyncClient
from langgraph.graph.state import CompiledStateGraph

from boba.chainlit2.agent import build_graph
from boba.chainlit2.agent.dump import DumpingTransport
from boba.chainlit2.infra.config import AppConfig, get_app_config
from boba.chainlit2.infra.di import Depend


def config() -> AppConfig:
    """Конфиг приложения (singleton)."""
    return get_app_config()


Cfg = Annotated[AppConfig, Depend(config)]


def openai_transport_options(cc: Cfg) -> dict:
    """Общие httpx-параметры транспорта из OpenAiConfig."""
    c = cc.profile.openai
    limits = httpx.Limits(
        max_connections=c.max_connections,
        max_keepalive_connections=c.max_keepalive_connections,
        keepalive_expiry=c.keepalive_expiry,
    )
    verify = httpx.create_ssl_context(verify=c.ssl_verify, cert=None, trust_env=True)
    socket_options = [
        # SO_KEEPALIVE = 1 - включить TCP keepalive
        # SO_KEEPALIVE = 0 - не использовать TCP keepalive
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, int(c.tcp_keepalive)),
    ]
    if c.tcp_keepalive:
        socket_options += [
            # файрволы выкидывают соединения, которые не пересылают пакеты по
            # установленному соединению обычно файрволны использовают параметры
            # в район 5-10 минут и здесь мы защищяемся от произвольного
            # выкидывания нашего соединения третьей стороной
            # 1. если соединение простаивает TCP_KEEPIDLE секунд ядро ОС будет
            #    слать пробу
            # 2. если удаленная сторона жива, ее ядро (НЕ программа, а именно
            #    linux) отвечает ASK
            # 3. если ответ нет значит ядро будет повторять еще TCP_KEEPCNT раз
            #    через каждые TCP_KEEPINTVL
            # Соединение объявляется мертвым и при следующей попытке
            # чтения/записи сокета будет получена ошибка ECONNABORTED/ETIMEDOUT
            (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, c.tcp_keepidle),
            (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, c.tcp_keepintvl),
            (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, c.tcp_keepcnt),
            # Если включить SO_KEEPALIVE и не указать остальные параметры, то
            # они будут взят из sysctl и обычно по умолчанию это
            # /proc/sys/net/ipv4/tcp_keepalive_time = 7200 (2 часа простоя!)
        ]

    return {
        "http2": False,
        "verify": verify,
        "limits": limits,
        "proxy": None,
        "retries": c.retries,
        "socket_options": socket_options,
    }


def httpx_timeout(cc: Cfg) -> httpx.Timeout:
    """AsyncOpenAI поверх готового транспорта; таймауты из OpenAiConfig."""
    c = cc.profile.openai
    return httpx.Timeout(
        connect=c.connect_timeout,
        read=c.read_timeout,
        write=c.write_timeout,
        pool=c.pool_timeout,
    )


def httpx_client(c: Cfg):
    return AsyncClient(
        timeout=httpx_timeout(c),
        transport=httpx.AsyncHTTPTransport(**openai_transport_options(c)),
    )


async def httpx_debug_client(
    c: Cfg,
) -> AsyncIterator[AsyncClient]:
    from chainlit.context import ChainlitContextException, get_context  # noqa: PLC0415

    def chainlit_filename(request: httpx.Request) -> str:
        """Метка запроса из контекста chainlit: пользователь + id сообщения."""
        try:
            ctx = get_context()
        except ChainlitContextException:
            return "no-context"

        who = "anon"
        if user := getattr(ctx.session, "user", None):
            who = user.identifier

        # parent_id у run-step'а on_message — это id входящего сообщения;
        # вне сообщения (например, on_chat_start) остаётся id сессии
        what = ctx.session.id
        if run := ctx.current_run:
            what = run.parent_id or run.id

        label = re.sub(r"[^\w.@-]", "_", f"{who}-{what}")

        res = f"{label}-{request.url.host}.log"

        return res

    transport = DumpingTransport(
        dump_dir=Path("dumps"),
        dump_file=chainlit_filename,
        **openai_transport_options(c),
    )

    client = AsyncClient(
        timeout=httpx_timeout(c),
        transport=transport,
    )

    try:
        yield client
    finally:
        pass


def client_settings(c: Cfg) -> dict:
    """Параметры запроса к LLM (model/temperature/...) из конфига."""
    return {
        "model": c.profile.model,
        "temperature": c.temperature,
        "max_tokens": c.max_tokens,
        "top_p": c.top_p,
        "frequency_penalty": c.frequency_penalty,
        "presence_penalty": c.presence_penalty,
        "stop": c.stop,
    }


def langchain_graph(
    c: Cfg,
    client: Annotated[AsyncClient, Depend(httpx_debug_client)],
) -> CompiledStateGraph:
    """Скомпилированный agent-граф под конфиг; scope задаёт потребитель (Depend)."""
    return build_graph(c, client)
