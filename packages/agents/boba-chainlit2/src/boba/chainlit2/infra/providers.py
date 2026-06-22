import re
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

import httpx
from httpx import AsyncClient
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg import AsyncConnection, sql
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from pydantic import SecretStr

from boba.agent.tool_config import (
    bind,
    build_app_config,
)
from boba.chainlit2.agent.dump import DumpingTransport
from boba.chainlit2.agent.tools import get_weather
from boba.chainlit2.errors import InternalServiceError
from boba.chainlit2.infra.config import (
    AgentProfile,
    AppConfig,
    OpenAiConfig,
    PostgresConfig,
)
from boba.chainlit2.infra.di import Depends


def get_app_config(config_path: Path) -> AppConfig:
    """Конфиг приложения"""
    return bind(build_app_config(config_path=config_path), path="app", model=AppConfig)


def get_store_config(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> PostgresConfig:
    return app_config.checkpoints


def get_agent_profile(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> AgentProfile:
    return app_config.agent


def get_openai_config(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> OpenAiConfig:
    return app_config.agent.openai


def _openai_transport_options(c: OpenAiConfig) -> dict:
    """Общие httpx-параметры транспорта из OpenAiConfig."""
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


def httpx_timeout(c: OpenAiConfig) -> httpx.Timeout:
    """AsyncOpenAI поверх готового транспорта; таймауты из OpenAiConfig."""
    return httpx.Timeout(
        connect=c.connect_timeout,
        read=c.read_timeout,
        write=c.write_timeout,
        pool=c.pool_timeout,
    )


def httpx_client(c: Annotated[AppConfig, Depends(get_app_config)]):
    return AsyncClient(
        timeout=httpx_timeout(c.agent.openai),
        transport=httpx.AsyncHTTPTransport(**_openai_transport_options(c.agent.openai)),
    )


async def httpx_debug_client(
    c: Annotated[AppConfig, Depends(get_app_config)],
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
        **_openai_transport_options(c.agent.openai),
    )

    client = AsyncClient(
        timeout=httpx_timeout(c.agent.openai),
        transport=transport,
    )

    try:
        yield client
    finally:
        pass


async def langchain_checkpoint_saver(
    c: Annotated[PostgresConfig, Depends(get_store_config)],
) -> AsyncIterator[BaseCheckpointSaver]:
    """PG-пул + checkpointer-saver; app-scope, пул закрывается на teardown DI."""
    pg_kwargs = c.to_pg_conn()
    # saver требует dict-строки; options (вкл. search_path) уже собран в to_pg_conn
    pg_kwargs.update({"row_factory": dict_row})

    pool_kwargs = c.to_pg_pool()

    async with AsyncConnectionPool(
        connection_class=AsyncConnection[DictRow],
        kwargs=pg_kwargs,
        # fail-fast: при недоступной БД getconn упадёт за timeout, а не за 30с
        **pool_kwargs,
    ) as pool:
        await pool.open()
        # saver делает только CREATE TABLE — схему из search_path создаём сами,
        # иначе setup() упадёт 'no schema has been selected to create in'
        if schema := c.options.primary_schema:
            try:
                async with pool.connection() as conn:
                    try:
                        await conn.execute(
                            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                                sql.Identifier(schema)
                            )
                        )
                    except InsufficientPrivilege as _e:
                        await conn.commit()
            except PoolTimeout as e:
                raise InternalServiceError(
                    internal_detail=(
                        "Failed to get postgres connection for langchain "
                        f"checkpoint saver: {e!s}"
                    ),
                    user_detail="Failed to connect to the internal postgres",
                ) from e
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        yield saver


def langchain_agent(
    c: Annotated[AppConfig, Depends(get_app_config)],
    client: Annotated[AsyncClient, Depends(httpx_debug_client)],
    saver: Annotated[BaseCheckpointSaver, Depends(langchain_checkpoint_saver)],
) -> CompiledStateGraph:
    chat = ChatOpenAI(
        http_async_client=client,
        model=c.agent.model,
        base_url=c.agent.openai.base_url,
        api_key=SecretStr(c.agent.openai.api_key),
        temperature=c.agent.temperature,
    )

    system_prompt = c.agent.default_system_prompt

    agent = create_agent(
        model=chat,
        tools=[get_weather],
        system_prompt=system_prompt,
        checkpointer=saver,
    )

    return agent
