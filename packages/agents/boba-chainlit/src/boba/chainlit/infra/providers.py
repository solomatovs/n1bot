"""Провайдеры зависимостей: конфиг, клиенты, хранилища и сам агент langgraph."""

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
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from omegaconf import DictConfig
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from psycopg_pool import PoolTimeout
from pydantic import SecretStr

from boba.chainlit.agent.chat_model import ReasoningChatOpenAI
from boba.chainlit.agent.dump import DumpingTransport
from boba.chainlit.chat.transcript import CheckpointMessages, TranscriptFeed
from boba.chainlit.connections import ConnectionsConfig, ConnectionStore
from boba.chainlit.data import PostgresDataLayer
from boba.chainlit.data.storage import StorageClient, StorageFactory
from boba.chainlit.domain.errors import InternalServiceError
from boba.chainlit.domain.keys import AttachmentLinks
from boba.chainlit.domain.session import current_user_roles
from boba.chainlit.infra.config import (
    AgentProfile,
    AppConfig,
    CheckpointerConfig,
    DataLayerConfig,
    LocalStorageConfig,
    OpenAiConfig,
    StreamJournalConfig,
)
from boba.chainlit.infra.di import Depends
from boba.chainlit.infra.plugins import PluginMeta, ToolRegistry, load_tools
from boba.db.pgvector import KbSchema
from boba.db.postgres import AsyncPostgresPool
from boba.sandbox import CgroupManager
from boba.sandbox.journal import DirVault, StreamJournal, StreamJournalHub
from boba.settings import bind, build_app_config
from boba.tool.kb import PostgresKnowledgeBaseConfig

_RAW_CONFIG: dict[str, DictConfig] = {}


def get_raw_config() -> DictConfig:
    try:
        return _RAW_CONFIG["config"]
    except KeyError:
        msg = "raw config is not initialised: call get_app_config() first"
        raise RuntimeError(msg) from None


def get_app_config(config_path: Path) -> AppConfig:
    raw = build_app_config(config_path=config_path)
    _RAW_CONFIG["config"] = raw
    config = bind(raw, path="app", model=AppConfig)
    # групповые лимиты проверяются на старте: отказ виден сразу, с именем профиля
    CgroupManager.probe_profiles(config.sandbox.profiles)
    return config


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


def get_stream_journal_config(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> StreamJournalConfig:
    return app_config.stream_journal


def stream_journal(
    cfg: Annotated[StreamJournalConfig, Depends(get_stream_journal_config)],
) -> StreamJournal:
    """Журнал вывода инструментов: том проверяется на старте, не при вызове.

    Журнал один на приложение, и его адресуют слои без DI (раздача файлов,
    tools уборки тома), поэтому он же кладётся в StreamJournalHub.
    """
    vault = DirVault(cfg.dir)
    vault.ensure_root()

    journal = StreamJournal(vault, cfg.reserve_bytes)
    StreamJournalHub.configure(journal)

    return journal


def storage_provider(
    cfg: Annotated[LocalStorageConfig, Depends(get_local_storage_config)],
) -> StorageClient:
    return StorageFactory.create(cfg)


def get_agent_profile(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> AgentProfile:
    return app_config.agent


def tool_registry(
    raw: Annotated[DictConfig, Depends(get_raw_config)],
) -> ToolRegistry:
    return load_tools(raw)


def session_tools(
    registry: Annotated[ToolRegistry, Depends(tool_registry)],
) -> list[BaseTool]:
    return registry.for_roles(current_user_roles())


async def kb_schema(
    raw: Annotated[DictConfig, Depends(get_raw_config)],
) -> None:
    """Готовит таблицы базы знаний, если секция [tool.kb] включена."""
    meta = bind(raw, "tool.kb", PluginMeta)
    if not meta.enable:
        return
    cfg = bind(raw, "tool.kb", PostgresKnowledgeBaseConfig)
    await KbSchema(cfg, dim=cfg.embedding.dim).setup()


async def connection_store(
    raw: Annotated[DictConfig, Depends(get_raw_config)],
) -> ConnectionStore | None:
    cfg = bind(raw, "connections", ConnectionsConfig)
    if not cfg.enable:
        return None
    store = ConnectionStore(cfg)
    await store.setup()
    return store


def get_openai_config(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> OpenAiConfig:
    return app_config.agent.openai


def _openai_transport_options(c: OpenAiConfig) -> dict:
    limits = httpx.Limits(
        max_connections=c.max_connections,
        max_keepalive_connections=c.max_keepalive_connections,
        keepalive_expiry=c.keepalive_expiry,
    )
    verify = httpx.create_ssl_context(verify=c.ssl_verify, cert=None, trust_env=True)
    socket_options = [
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, int(c.tcp_keepalive)),
    ]
    if c.tcp_keepalive:
        socket_options += [
            (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, c.tcp_keepidle),
            (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, c.tcp_keepintvl),
            (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, c.tcp_keepcnt),
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
    return httpx.Timeout(
        connect=c.connect_timeout,
        read=c.read_timeout,
        write=c.write_timeout,
        pool=c.pool_timeout,
    )


def _openai_dump_transport(c: AppConfig) -> DumpingTransport:
    from chainlit.context import ChainlitContextException, get_context  # noqa: PLC0415

    def chainlit_filename(request: httpx.Request) -> str:
        try:
            ctx = get_context()
        except ChainlitContextException:
            return f"no-context-{request.url.host}.log"

        who = "anon"
        if user := getattr(ctx.session, "user", None):
            who = user.identifier

        label = re.sub(r"[^\w.@-]", "_", f"{who}-{ctx.session.thread_id}")

        return f"{label}-{request.url.host}.log"

    return DumpingTransport(
        dump_dir=Path(c.agent.openai.dump.path),
        dump_file=chainlit_filename,
        **_openai_transport_options(c.agent.openai),
    )


def httpx_client(c: Annotated[AppConfig, Depends(get_app_config)]) -> AsyncClient:
    if c.agent.openai.dump.enable:
        transport = _openai_dump_transport(c)
    else:
        transport = httpx.AsyncHTTPTransport(
            **_openai_transport_options(c.agent.openai)
        )

    return AsyncClient(
        timeout=httpx_timeout(c.agent.openai),
        transport=transport,
    )


async def langchain_checkpoint_saver(
    cp: Annotated[CheckpointerConfig, Depends(get_checkpointer_config)],
) -> AsyncIterator[BaseCheckpointSaver]:
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


async def chainlit_data_layer(
    cfg: Annotated[DataLayerConfig, Depends(get_data_layer_config)],
    storage_cfg: Annotated[LocalStorageConfig, Depends(get_local_storage_config)],
    storage: Annotated[StorageClient, Depends(storage_provider)],
    saver: Annotated[BaseCheckpointSaver, Depends(langchain_checkpoint_saver)],
) -> AsyncIterator[PostgresDataLayer]:
    pool = AsyncPostgresPool(
        cfg.postgres,
        override_options={"search_path": cfg.db_schema},
    )
    await pool.open()
    try:
        layer = PostgresDataLayer(
            pool,
            schema=cfg.db_schema,
            storage=storage,
            feed=TranscriptFeed(CheckpointMessages(saver)),
            links=AttachmentLinks(storage_cfg.public_prefix),
        )
        await layer.setup()
        yield layer
    finally:
        await pool.close()


def build_history_view(allowed_tools: frozenset[str], history_messages: int):
    @wrap_model_call
    async def history_view(request: ModelRequest, handler):
        full = request.state["messages"]
        view = build_llm_view(full, allowed_tools, history_messages)
        return await handler(request.override(messages=view))

    return history_view


def build_llm_view(
    msgs: list,
    allowed_tools: frozenset[str] | None = None,
    history_messages: int = AgentProfile.model_fields["history_messages"].default,
) -> list:
    start = _index_of_last_user_turn(msgs)
    head, current = msgs[:start], msgs[start:]

    replies: list = []
    for message in head:
        if isinstance(message, ToolMessage):
            continue
        if isinstance(message, AIMessage) and message.tool_calls:
            continue
        replies.append(message)

    view = replies[-history_messages:] + _drop_foreign_tools(current, allowed_tools)
    return [_with_attachments(m) for m in view]


def _with_attachments(message: object) -> object:
    """Дописывает пути вложений в текст: в ленте их быть не должно."""
    if not isinstance(message, HumanMessage):
        return message
    attachments = message.additional_kwargs.get("attachments") or []
    if not attachments:
        return message
    listing = "\n".join(f"- {a['name']}: {a['path']}" for a in attachments)
    return message.model_copy(
        update={
            "content": (
                f"{message.content}\n\n"
                f"Прикреплённые файлы, доступны инструменту bash по этим путям:\n"
                f"{listing}"
            )
        }
    )


def _drop_foreign_tools(msgs: list, allowed_tools: frozenset[str] | None) -> list:
    if allowed_tools is None:
        return msgs

    dropped_ids: set[str] = set()
    kept: list = []
    for m in msgs:
        if isinstance(m, AIMessage) and m.tool_calls:
            foreign = [c for c in m.tool_calls if c["name"] not in allowed_tools]
            if foreign:
                dropped_ids.update(c["id"] for c in m.tool_calls if c["id"])
                continue
        if isinstance(m, ToolMessage) and m.tool_call_id in dropped_ids:
            continue
        kept.append(m)
    return kept


def _index_of_last_user_turn(msgs: list) -> int:
    for i in range(len(msgs) - 1, -1, -1):
        if isinstance(msgs[i], HumanMessage):
            return i

    return 0


def langchain_agent(
    c: Annotated[AppConfig, Depends(get_app_config)],
    client: Annotated[AsyncClient, Depends(httpx_client)],
    saver: Annotated[BaseCheckpointSaver, Depends(langchain_checkpoint_saver)],
    tools: Annotated[list[BaseTool], Depends(session_tools, scope="session")],
) -> CompiledStateGraph:
    chat = ReasoningChatOpenAI(
        http_async_client=client,
        model=c.agent.model,
        base_url=c.agent.openai.base_url,
        api_key=SecretStr(c.agent.openai.api_key),
        temperature=c.agent.temperature,
    )

    system_prompt = c.agent.default_system_prompt

    agent = create_agent(
        model=chat,
        tools=tools,
        system_prompt=system_prompt,
        checkpointer=saver,
        middleware=[
            build_history_view(
                frozenset(t.name for t in tools), c.agent.history_messages
            )
        ],
    )

    return agent
