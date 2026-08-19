"""Провайдеры зависимостей: конфиг, клиенты, хранилища и сам агент langgraph."""

import re
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Annotated

import httpx
from httpx import AsyncClient
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

from boba.chainlit.agent.flow import (
    AgentGraphBuilder,
    GraphSpec,
    LlmRephraser,
    PassthroughRephraser,
    PlainGraphBuilder,
    PrefetchGraphBuilder,
    Rephraser,
)
from boba.chainlit.chat.history import CheckpointMessages, TranscriptFeed
from boba.chainlit.chat.tracing import TracedStage
from boba.chainlit.connections import ConnectionsConfig, ConnectionStore
from boba.chainlit.data import PostgresDataLayer
from boba.chainlit.data.storage import StorageClient, StorageFactory
from boba.chainlit.domain.errors import InternalServiceError
from boba.chainlit.domain.keys import AttachmentLinks
from boba.chainlit.domain.session import (
    current_chat_profile,
    current_user_metadata,
    current_user_roles,
)
from boba.chainlit.infra.config import (
    AgentSettings,
    AppConfig,
    ChatProfiles,
    CheckpointerConfig,
    DataLayerConfig,
    LocalStorageConfig,
    PrefetchFlowConfig,
    SelectedProfile,
    SettingsView,
    UserMeta,
)
from boba.chainlit.infra.di import Depends
from boba.chainlit.infra.plugins import PluginMeta, ToolRegistry, load_tools
from boba.chainlit.rendering.chat_view import StepText
from boba.db.pgvector import KbSchema
from boba.db.postgres import AsyncPostgresPool
from boba.llm.chat import ReasoningChatOpenAI
from boba.llm.openai import OpenAiConfig, OpenAiHttp
from boba.sandbox import CgroupManager
from boba.settings import bind, build_app_config
from boba.tool.kb.kb import PostgresKnowledgeBaseConfig

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


def storage_provider(
    cfg: Annotated[LocalStorageConfig, Depends(get_local_storage_config)],
) -> StorageClient:
    return StorageFactory.create(cfg)


def chat_profiles_registry(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
) -> ChatProfiles:
    return ChatProfiles(app_config.profiles)


def session_profile(
    registry: Annotated[ChatProfiles, Depends(chat_profiles_registry)],
) -> SelectedProfile:
    """Профиль текущей сессии; без доступного профиля — отказ.

    Ошибки:
    RefusalError — профиль не выбран или недоступен ролям пользователя.
    """
    return registry.resolve(current_chat_profile(), current_user_roles())


def session_settings_view(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
) -> SettingsView:
    """Профиль сессии, поверх которого легли личные настройки пользователя."""
    meta = UserMeta.of(current_user_metadata())
    return SettingsView.of(
        app_config.settings,
        selected.config,
        meta.overrides_for(selected.name),
    )


def session_agent_settings(
    view: Annotated[SettingsView, Depends(session_settings_view, scope="session")],
) -> AgentSettings:
    """Настройки, с которыми идёт ход."""
    return view.agent()


def tool_registry(
    raw: Annotated[DictConfig, Depends(get_raw_config)],
) -> ToolRegistry:
    return load_tools(raw)


def session_tools(
    registry: Annotated[ToolRegistry, Depends(tool_registry)],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
) -> list[BaseTool]:
    return registry.for_session(current_user_roles(), selected.name)


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


def _chainlit_dump_file(request: httpx.Request) -> str:
    """Имя файла дампа: пользователь и thread текущей chainlit-сессии."""
    from chainlit.context import ChainlitContextException, get_context  # noqa: PLC0415

    try:
        ctx = get_context()
    except ChainlitContextException:
        return f"no-context-{request.url.host}.log"

    who = "anon"
    if user := getattr(ctx.session, "user", None):
        who = user.identifier

    label = re.sub(r"[^\w.@-]", "_", f"{who}-{ctx.session.thread_id}")

    return f"{label}-{request.url.host}.log"


def _openai_client(openai: OpenAiConfig) -> AsyncClient:
    dump_file = None
    if openai.dump.enable:
        dump_file = _chainlit_dump_file

    return OpenAiHttp.client(openai, dump_file)


async def httpx_clients(
    c: Annotated[AppConfig, Depends(get_app_config)],
) -> AsyncIterator[dict[str, AsyncClient]]:
    """HTTP-клиент провайдера на каждый профиль чата; живут до остановки.

    Prefetch-flow профиля получает свой клиент под ключом flow: у его
    переформулировщика свой транспорт.
    """
    clients: dict[str, AsyncClient] = {}
    for name, profile in c.profiles.items():
        clients[name] = _openai_client(profile.provider)

        flow = profile.flow
        if not isinstance(flow, PrefetchFlowConfig):
            continue

        if flow.rephraser is None:
            continue

        clients[flow.client_key(name)] = _openai_client(flow.rephraser.provider)

    try:
        yield clients
    finally:
        for client in clients.values():
            await client.aclose()


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
    history_messages: int = AgentSettings.model_fields["history_messages"].default,
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


def _flow_tools(names: Sequence[str], tools: Sequence[BaseTool]) -> list[BaseTool]:
    """Инструменты flow среди доступных сессии; чужое имя — отказ сборки."""
    by_name: dict[str, BaseTool] = {}
    for tool in tools:
        by_name[tool.name] = tool

    selected: list[BaseTool] = []
    for name in names:
        found = by_name.get(name)
        if found is None:
            msg = f"flow tool {name!r} is not available to the session"
            raise RuntimeError(msg)

        selected.append(found)

    return selected


def _graph_builder(
    selected: SelectedProfile,
    clients: dict[str, AsyncClient],
    tools: Sequence[BaseTool],
) -> AgentGraphBuilder:
    """Билдер графа хода по flow профиля."""
    flow = selected.config.flow
    if not isinstance(flow, PrefetchFlowConfig):
        return PlainGraphBuilder()

    rephraser = _rephraser(flow, clients, selected.name)
    stage = TracedStage(StepText.PREFETCH.value)
    return PrefetchGraphBuilder(rephraser, _flow_tools(flow.tools, tools), stage)


def _rephraser(
    flow: PrefetchFlowConfig,
    clients: dict[str, AsyncClient],
    profile: str,
) -> Rephraser:
    """Модель, готовящая поисковые запросы; без секции — запрос идёт как есть."""
    cfg = flow.rephraser
    if cfg is None:
        return PassthroughRephraser()

    # стрим переформулировщику не нужен: его ответ забирает не лента, а
    # подготовка контекста, и рассуждения модели в ленту попадать не должны
    chat = ReasoningChatOpenAI(
        http_async_client=clients[flow.client_key(profile)],
        model=cfg.model,
        base_url=cfg.provider.base_url,
        api_key=SecretStr(cfg.provider.api_key),
        timeout=OpenAiHttp.timeout(cfg.provider),
        max_retries=cfg.provider.max_retries,
        disable_streaming=True,
        **cfg.chat_kwargs(),
    )

    return LlmRephraser(chat, cfg.system_prompt, cfg.queries)


def langchain_agent(
    clients: Annotated[dict[str, AsyncClient], Depends(httpx_clients)],
    saver: Annotated[BaseCheckpointSaver, Depends(langchain_checkpoint_saver)],
    tools: Annotated[list[BaseTool], Depends(session_tools, scope="session")],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
    settings: Annotated[
        AgentSettings, Depends(session_agent_settings, scope="session")
    ],
) -> CompiledStateGraph:
    # таймаут запроса клиент openai ставит поверх клиентского: без него в
    # httpx уходит None и фазы остаются без потолка
    stream_chunk_timeout = None
    if settings.provider.stream_chunk_timeout:
        stream_chunk_timeout = settings.provider.stream_chunk_timeout

    chat = ReasoningChatOpenAI(
        http_async_client=clients[selected.name],
        model=settings.model,
        base_url=settings.provider.base_url,
        api_key=SecretStr(settings.provider.api_key),
        timeout=OpenAiHttp.timeout(settings.provider),
        stream_chunk_timeout=stream_chunk_timeout,
        max_retries=settings.provider.max_retries,
        **settings.chat_kwargs(),
    )

    names: list[str] = []
    for tool in tools:
        names.append(tool.name)

    spec = GraphSpec(
        chat=chat,
        tools=tools,
        system_prompt=settings.system_prompt,
        checkpointer=saver,
        history=build_history_view(frozenset(names), settings.history_messages),
    )

    return _graph_builder(selected, clients, tools).build(spec)
