import re
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from httpx import AsyncClient
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from boba.agent.tool_config import (
    bind,
    build_app_config,
)
from boba.hermes.agent.api import HermesApi
from boba.hermes.agent.dump import DumpingTransport
from boba.hermes.chat.data import (
    HermesDataLayer,
    HermesProfileRepository,
    PostgresDataLayer,
)
from boba.hermes.chat.data.storage import LocalStorageClient
from boba.hermes.infra.config import (
    AppConfig,
    DataLayerConfig,
    HermesConfig,
    LocalStorageConfig,
    PostgresConfig,
)
from boba.hermes.infra.di import Depends


def get_app_config(config_path: Path) -> AppConfig:
    """Конфиг приложения"""
    return bind(build_app_config(config_path=config_path), path="app", model=AppConfig)


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


def get_hermes_config(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> HermesConfig:
    return app_config.hermes


async def data_layer_pool(
    cfg: Annotated[DataLayerConfig, Depends(get_data_layer_config)],
) -> AsyncIterator[AsyncConnectionPool]:
    """Общий пул postgres: и data layer, и связка профилей ходят в одну базу."""
    async with postgres_pool(cfg.postgres, cfg.db_schema) as pool:
        yield pool


def hermes_profile_repository(
    pool: Annotated[AsyncConnectionPool, Depends(data_layer_pool)],
    cfg: Annotated[DataLayerConfig, Depends(get_data_layer_config)],
) -> HermesProfileRepository:
    """Связка пользователя chainlit с профилем hermes."""
    return HermesProfileRepository(pool, schema=cfg.db_schema)


async def hermes_http_client(
    c: Annotated[AppConfig, Depends(get_app_config)],
) -> AsyncIterator[AsyncClient]:
    """Соединение с api_server: один пул на приложение.

    При hermes.dump = true запросы и ответы дополнительно пишутся на диск —
    так видно, что именно ушло в api_server и что он ответил.
    """
    transport = (
        _dumping_transport(c)
        if c.hermes.dump
        else httpx.AsyncHTTPTransport(**_transport_options(c.hermes))
    )
    client = AsyncClient(timeout=_httpx_timeout(c.hermes), transport=transport)

    try:
        yield client
    finally:
        await client.aclose()


def hermes_api(
    client: Annotated[AsyncClient, Depends(hermes_http_client)],
    cfg: Annotated[HermesConfig, Depends(get_hermes_config)],
) -> HermesApi:
    """Фабрика клиентов api_server по профилям."""
    return HermesApi(client, cfg)


def _transport_options(c: HermesConfig) -> dict:
    """httpx-параметры транспорта api_server."""
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


def _httpx_timeout(c: HermesConfig) -> httpx.Timeout:
    """Таймауты api_server; ход агента идёт долгим SSE-стримом."""
    return httpx.Timeout(
        connect=c.connect_timeout,
        read=c.read_timeout,
        write=c.write_timeout,
        pool=c.pool_timeout,
    )


def _dumping_transport(c: AppConfig) -> DumpingTransport:
    """Транспорт, пишущий запрос и ответ в файл на пользователя и сообщение."""
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

    return DumpingTransport(
        dump_dir=Path(Path(c.chainlit.root) / "dump"),
        dump_file=chainlit_filename,
        **_transport_options(c.hermes),
    )


@asynccontextmanager
async def postgres_pool(
    c: PostgresConfig,
    schema: str,
) -> AsyncIterator[AsyncConnectionPool]:
    """
    Helper для создания PG-пул
    """
    async with AsyncConnectionPool(
        connection_class=AsyncConnection,
        kwargs=c.conn_settings({"search_path": schema}),
        **c.pool_settings(),
    ) as pool:
        await pool.open()
        yield pool


async def chainlit_data_layer(
    pool: Annotated[AsyncConnectionPool, Depends(data_layer_pool)],
    cfg: Annotated[DataLayerConfig, Depends(get_data_layer_config)],
    storage: Annotated[LocalStorageClient, Depends(storage_provider)],
) -> PostgresDataLayer:
    """PostgresDataLayer на общем пуле; setup() создаёт схему и таблицы."""
    layer = PostgresDataLayer(pool, schema=cfg.db_schema, storage=storage)
    await layer.setup()

    return layer


async def hermes_data_layer(
    storage: Annotated[PostgresDataLayer, Depends(chainlit_data_layer)],
    profiles: Annotated[HermesProfileRepository, Depends(hermes_profile_repository)],
    api: Annotated[HermesApi, Depends(hermes_api)],
) -> HermesDataLayer:
    """Data layer chainlit: история из hermes, остальное из postgres."""
    return HermesDataLayer(storage=storage, profiles=profiles, api=api)

