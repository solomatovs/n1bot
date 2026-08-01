import re
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

import httpx
from httpx import AsyncClient
from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, wrap_model_call
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from psycopg_pool import PoolTimeout
from pydantic import SecretStr

from boba.agent.tool_config import (
    bind,
    build_app_config,
)
from boba.auth.errors import InternalServiceError
from boba.chainlit2.agent.dump import DumpingTransport
from boba.chainlit2.agent.tools import get_weather
from boba.chainlit2.chat.data import PostgresDataLayer
from boba.chainlit2.chat.data.storage import LocalStorageClient
from boba.chainlit2.infra.config import (
    AgentProfile,
    AppConfig,
    CheckpointerConfig,
    DataLayerConfig,
    LocalStorageConfig,
    OpenAiConfig,
)
from boba.chainlit2.infra.di import Depends
from boba.db.postgres import AsyncPostgresPool


def get_app_config(config_path: Path) -> AppConfig:
    """Конфиг приложения"""
    return bind(build_app_config(config_path=config_path), path="app", model=AppConfig)


def get_checkpointer_config(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> CheckpointerConfig:
    return app_config.checkpointer


def get_data_layer_config(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> DataLayerConfig:
    return app_config.data_layer


def get_local_storage_config(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> LocalStorageConfig:
    return app_config.storage


def storage_provider(
    cfg: Annotated[LocalStorageConfig, Depends(get_local_storage_config)],
) -> LocalStorageClient:
    """Файловое хранилище вложений (локальный диск)."""
    return LocalStorageClient(cfg)


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
        dump_dir=Path(Path(c.chainlit.root) / "dump"),
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


async def chainlit_data_layer(
    cfg: Annotated[DataLayerConfig, Depends(get_data_layer_config)],
    storage: Annotated[LocalStorageClient, Depends(storage_provider)],
) -> AsyncIterator[PostgresDataLayer]:
    """PostgresDataLayer на своём пуле; setup() создаёт схему и таблицы."""
    pool = AsyncPostgresPool(
        cfg.postgres,
        override_options={"search_path": cfg.db_schema},
    )
    await pool.open()
    try:
        layer = PostgresDataLayer(pool, schema=cfg.db_schema, storage=storage)
        await layer.setup()
        yield layer
    finally:
        await pool.close()


async def langchain_checkpoint_saver(
    cp: Annotated[CheckpointerConfig, Depends(get_checkpointer_config)],
) -> AsyncIterator[BaseCheckpointSaver]:
    """
    AsyncPostgresSaver на своём пуле (search_path=схема)
    """
    pool = AsyncPostgresPool(
        cp.postgres,
        override_options={"search_path": cp.db_schema},
    )
    await pool.open()
    try:
        try:
            async with pool.connection() as conn:
                try:
                    await conn.execute(
                        sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                            sql.Identifier(cp.db_schema)
                        )
                    )
                except InsufficientPrivilege:
                    await conn.commit()
        except PoolTimeout as e:
            raise InternalServiceError(
                internal_detail=(
                    f"Failed to get postgres connection for checkpointer: {e!s}"
                ),
                user_detail="Failed to connect to the internal postgres",
            ) from e

        saver = AsyncPostgresSaver(pool.raw)
        await saver.setup()
        yield saver
    finally:
        await pool.close()


@wrap_model_call
async def history_view(request: ModelRequest, handler):
    """View-слой: проекция полной истории в то, что видит LLM (state не меняется).

    Async: агент запускается через astream()/ainvoke(), поэтому langchain зовёт
    awrap_model_call, и handler здесь — awaitable.
    """
    # полная история находится в state
    full = request.state["messages"]
    # проекция видимости истории llm на каждой итерации
    view = build_llm_view(full)
    return await handler(request.override(messages=view))


def build_llm_view(msgs: list) -> list:
    """
    Представление над историей чата для llm реквеста

    Старые сообщения отдает только как вопросы/ответы без tool_call/tool_result
    Текущая итерация отдается полностью
    """
    start = _index_of_last_user_turn(msgs)
    head, current = msgs[:start], msgs[start:]

    pruned_head = [
        m
        for m in head
        if not isinstance(m, ToolMessage)
        and not (isinstance(m, AIMessage) and m.tool_calls)
    ][-30:]
    return pruned_head + current


def _index_of_last_user_turn(msgs: list) -> int:
    """Индекс начала последней итерации = позиция последнего HumanMessage."""
    for i in range(len(msgs) - 1, -1, -1):
        if isinstance(msgs[i], HumanMessage):
            return i

    return 0


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
        middleware=[history_view],
    )

    return agent
