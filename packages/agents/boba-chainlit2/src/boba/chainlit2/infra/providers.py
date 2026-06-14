import re
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

import httpx
from httpx import AsyncClient
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from boba.agent.tool_config import (
    bind,
    build_app_config,
)
from boba.chainlit2.agent import build_agent, build_langgraph
from boba.chainlit2.agent.dump import DumpingTransport
from boba.chainlit2.infra.config import AppConfig
from boba.chainlit2.infra.di import Depends


def get_app_config() -> AppConfig:
    """Конфиг приложения (singleton)."""
    return bind(build_app_config(), path="chainlit2", model=AppConfig)


Cfg = Annotated[AppConfig, Depends(get_app_config)]


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


async def langchain_checkpoint_saver(c: Cfg) -> AsyncIterator[BaseCheckpointSaver]:
    """PG-пул + checkpointer-saver; app-scope, пул закрывается на teardown DI."""
    async with AsyncConnectionPool(
        # пустой conninfo ⇒ libpq берёт параметры из env (PGHOST/...)
        conninfo=c.checkpoint_dsn or "",
        # saver требует dict-строки; connection_class фиксирует тип пула как
        # AsyncConnection[DictRow], иначе инвариантность не сходится с Conn
        connection_class=AsyncConnection[DictRow],
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        # fail-fast: при недоступной БД getconn упадёт за timeout, а не за 30с
        timeout=c.checkpoint_pool_timeout,
        open=False,
    ) as pool:
        await pool.open()
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        yield saver


def langchain_graph(
    c: Cfg,
    client: Annotated[AsyncClient, Depends(httpx_debug_client)],
) -> CompiledStateGraph:
    """Скомпилированный agent-граф под конфиг; scope задаёт потребитель (Depend)."""
    return build_langgraph(c, client)


def langchain_agent(
    c: Cfg,
    client: Annotated[AsyncClient, Depends(httpx_debug_client)],
    saver: Annotated[BaseCheckpointSaver, Depends(langchain_checkpoint_saver)],
) -> CompiledStateGraph:
    return build_agent(c, client, saver)
