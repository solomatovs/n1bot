"""Провайдеры зависимостей: конфиг, клиенты, хранилища и сам агент langgraph."""

import re
import socket
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Annotated

import httpx
from httpx import AsyncClient
from langchain.agents.middleware import ModelRequest, wrap_model_call
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from omegaconf import DictConfig
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from psycopg_pool import PoolTimeout

from boba.chainlit.agent.flow import (
    AgentGraphBuilder,
    GraphSpec,
    LlmRephraser,
    PassthroughRephraser,
    PlainGraphBuilder,
    PrefetchGraphBuilder,
    Rephraser,
)
from boba.chainlit.auth.kerberos import KerberosAuth
from boba.chainlit.chat.history import CheckpointMessages, TranscriptFeed
from boba.chainlit.chat.tracing import TracedStage
from boba.chainlit.connections import ConnectionsConfig, ConnectionStore
from boba.chainlit.data import PostgresDataLayer
from boba.chainlit.data.storage import StorageClient, StorageFactory
from boba.chainlit.domain.errors import InternalServiceError
from boba.chainlit.domain.keys import AttachmentLinks
from boba.chainlit.domain.session import SessionSource
from boba.chainlit.infra.config import (
    AgentSettings,
    AppConfig,
    ChatProfiles,
    CheckpointerConfig,
    DataLayerConfig,
    LocalStorageConfig,
    PrefetchFlowConfig,
    RolesSection,
    SelectedProfile,
    SettingsView,
    UserMeta,
)
from boba.chainlit.infra.di import Container, Depends
from boba.chainlit.infra.plugins import PluginMeta, ToolRegistry, load_tools
from boba.chainlit.infra.session import ChainlitSessions, current_session
from boba.chainlit.rendering.chat_view import StepText
from boba.chainlit.workflow.events import RunEvents
from boba.chainlit.workflow.service import WorkflowService
from boba.chainlit.workflow.store import WorkflowConfig, WorkflowStore
from boba.chat.generation import LocalGeneration, OpenAiGeneration, StructuredGenerator
from boba.chat.openai import OpenAiConfig
from boba.chat.provider import ChatProvider, LocalChatConfig, OpenAiChatConfig
from boba.db.pgvector.schema import KbSchema
from boba.db.postgres import AsyncPostgresPool
from boba.krb import CcacheRegistry
from boba.llm.bridge import ChatProviderFactory, ProviderChatModel
from boba.llm.generation import GeneratorFactory
from boba.llm.local import OnnxChatRuntime
from boba.llm.openai import OpenAiHttp
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
    # кэши билетов раскладывает приложение: строкам соединений пути не задают
    config.krb.apply()

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
    session = current_session()

    return registry.resolve(session.chat_profile, session.roles)


def session_settings_view(
    app_config: Annotated[AppConfig, Depends(get_app_config)],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
) -> SettingsView:
    """Профиль сессии, поверх которого легли личные настройки пользователя."""
    meta = UserMeta.of(current_session().metadata)
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


def kerberos_auth() -> KerberosAuth | None:
    """SSO-провайдер kerberos; значение кладёт bootstrap после установки auth."""
    msg = "kerberos_auth is provided by bootstrap, not produced"
    raise RuntimeError(msg)


def ccache_registry_ref() -> CcacheRegistry | None:
    """Реестр делегированных тикетов; None — SSO kerberos не настроен."""
    root = Container.root
    if root is None:
        msg = "DI container is not initialised"
        raise RuntimeError(msg)

    auth = root.resolved(kerberos_auth)
    if auth is None:
        return None

    return auth.registry


def session_source() -> SessionSource:
    """Источник сессий приложения; реализация знает про chainlit."""
    return ChainlitSessions()


def connection_store_ref() -> ConnectionStore:
    """Хранилище соединений для обвязок инструментов; зовётся на каждый вызов."""
    root = Container.root
    if root is None:
        msg = "DI container is not initialised"
        raise RuntimeError(msg)

    store = root.resolved(connection_store)
    if store is None:
        msg = "[connections] is disabled: user connections are unavailable"
        raise RuntimeError(msg)

    return store


def tool_registry(
    raw: Annotated[DictConfig, Depends(get_raw_config)],
) -> ToolRegistry:
    return load_tools(raw, connection_store_ref, ccache_registry_ref)


async def tool_registry_ref() -> ToolRegistry:
    """Реестр инструментов из корневого контейнера: для вызовов вне сессии."""
    root = Container.root
    if root is None:
        msg = "DI container is not initialised: tool registry is unavailable"
        raise RuntimeError(msg)

    return await root.resolve(Depends(tool_registry))


async def workflow_service_ref() -> WorkflowService:
    """Сервис workflow из корневого контейнера; зовётся на каждый вызов."""
    root = Container.root
    if root is None:
        msg = "DI container is not initialised"
        raise RuntimeError(msg)

    service = await root.resolve(Depends(workflow_service))
    if service is None:
        msg = "[workflow] is disabled: workflows are unavailable"
        raise RuntimeError(msg)

    return service


def session_tools(
    registry: Annotated[ToolRegistry, Depends(tool_registry)],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
) -> list[BaseTool]:
    return registry.for_session(current_session().roles, selected.name)


async def kb_schema(
    raw: Annotated[DictConfig, Depends(get_raw_config)],
) -> None:
    """Готовит таблицы базы знаний, если секция [tool.kb] включена."""
    meta = bind(raw, "tool.kb", PluginMeta)
    if not meta.enable:
        return
    cfg = bind(raw, "tool.kb", PostgresKnowledgeBaseConfig)
    await KbSchema(cfg, dim=cfg.embedding.dim).setup()


async def workflow_store(
    raw: Annotated[DictConfig, Depends(get_raw_config)],
) -> WorkflowStore | None:
    """Хранилище workflow и их запусков; None — секция [workflow] выключена."""
    cfg = bind(raw, "workflow", WorkflowConfig)
    if not cfg.enable:
        return None

    store = WorkflowStore(cfg)
    await store.setup()

    return store


def workflow_service(
    store: Annotated[WorkflowStore | None, Depends(workflow_store)],
    config: Annotated[AppConfig, Depends(get_app_config)],
) -> WorkflowService | None:
    """Сервис workflow; инстанс — host:port, чтобы различать запуски реплик."""
    if store is None:
        return None

    instance = f"{socket.gethostname()}:{config.chainlit.port}"
    return WorkflowService(store, tool_registry_ref, instance, RunEvents())


async def connection_store(
    raw: Annotated[DictConfig, Depends(get_raw_config)],
) -> ConnectionStore | None:
    """Хранилище соединений; роли из [roles] попадают в таблицу roles на старте."""
    cfg = bind(raw, "connections", ConnectionsConfig)
    if not cfg.enable:
        return None

    store = ConnectionStore(cfg)
    await store.setup()

    roles = bind(raw, "roles", RolesSection).root
    await store.sync_roles(roles)

    return store


def _chainlit_dump_file(request: httpx.Request) -> str:
    """Имя файла дампа: пользователь и thread текущей chainlit-сессии."""
    thread_id = current_session().thread_id
    if thread_id is None:
        return f"no-context-{request.url.host}.log"

    who = current_session().label
    if not who:
        who = "anon"

    label = re.sub(r"[^\w.@-]", "_", f"{who}-{thread_id}")

    return f"{label}-{request.url.host}.log"


def _openai_client(openai: OpenAiConfig) -> AsyncClient:
    dump_file = None
    if openai.dump.enable:
        dump_file = _chainlit_dump_file

    return OpenAiHttp.client(openai, dump_file)


async def httpx_clients(
    c: Annotated[AppConfig, Depends(get_app_config)],
) -> AsyncIterator[dict[str, AsyncClient]]:
    """HTTP-клиент на каждый openai-бэкенд профиля чата; живут до остановки.

    Профиль на локальном бэкенде клиента не имеет. Prefetch-flow профиля
    получает свой клиент под ключом flow: у его переформулировщика свой
    транспорт.
    """
    clients: dict[str, AsyncClient] = {}
    for name, profile in c.profiles.items():
        if isinstance(profile.backend, OpenAiChatConfig):
            clients[name] = _openai_client(profile.backend.openai)

        flow = profile.flow
        if not isinstance(flow, PrefetchFlowConfig):
            continue

        if not isinstance(flow.rephraser, OpenAiGeneration):
            continue

        clients[flow.client_key(name)] = _openai_client(flow.rephraser.openai)

    try:
        yield clients
    finally:
        for client in clients.values():
            await client.aclose()


def local_chat_runtimes(
    c: Annotated[AppConfig, Depends(get_app_config)],
) -> dict[str, OnnxChatRuntime]:
    """Локальные рантаймы по каталогу модели; один экземпляр на процесс.

    Каталог собирается со всех профилей с локальным бэкендом и локальных
    переформулировщиков: одна и та же модель грузится один раз и обслуживает
    обе способности.
    """
    runtimes: dict[str, OnnxChatRuntime] = {}

    for profile in c.profiles.values():
        if isinstance(profile.backend, LocalChatConfig):
            model_dir = profile.backend.model_dir
            if model_dir not in runtimes:
                runtimes[model_dir] = OnnxChatRuntime(model_dir)

        flow = profile.flow
        if not isinstance(flow, PrefetchFlowConfig):
            continue

        if not isinstance(flow.rephraser, LocalGeneration):
            continue

        model_dir = flow.rephraser.model_dir
        if model_dir not in runtimes:
            runtimes[model_dir] = OnnxChatRuntime(model_dir)

    return runtimes


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
            sessions=session_source(),
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


def rephrase_generators(
    c: Annotated[AppConfig, Depends(get_app_config)],
    clients: Annotated[dict[str, AsyncClient], Depends(httpx_clients)],
    runtimes: Annotated[dict[str, OnnxChatRuntime], Depends(local_chat_runtimes)],
) -> dict[str, StructuredGenerator]:
    """Генератор переформулировок на профиль; живут до остановки приложения.

    Локальный бэкенд работает на общем рантайме local_chat_runtimes: модель
    грузится один раз на процесс и делится с чатом.
    """
    generators: dict[str, StructuredGenerator] = {}
    for name, profile in c.profiles.items():
        flow = profile.flow
        if not isinstance(flow, PrefetchFlowConfig):
            continue

        cfg = flow.rephraser
        if cfg is None:
            continue

        runtime = None
        if isinstance(cfg, LocalGeneration):
            runtime = runtimes[cfg.model_dir]

        generators[name] = GeneratorFactory.build(
            cfg,
            client=clients.get(flow.client_key(name)),
            runtime=runtime,
        )

    return generators


def session_graph_builder(
    generators: Annotated[
        Mapping[str, StructuredGenerator], Depends(rephrase_generators)
    ],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
    tools: Annotated[Sequence[BaseTool], Depends(session_tools, scope="session")],
) -> AgentGraphBuilder:
    """Билдер графа хода по flow профиля."""
    flow = selected.config.flow
    if not isinstance(flow, PrefetchFlowConfig):
        return PlainGraphBuilder()

    rephraser = _rephraser(generators, selected.name)
    stage = TracedStage(StepText.PREFETCH.value)
    return PrefetchGraphBuilder(rephraser, _flow_tools(flow.tools, tools), stage)


def _rephraser(
    generators: Mapping[str, StructuredGenerator],
    profile: str,
) -> Rephraser:
    """Модель, готовящая поисковые запросы; без секции — запрос идёт как есть."""
    generator = generators.get(profile)
    if generator is None:
        return PassthroughRephraser()

    return LlmRephraser(generator)


def session_chat_provider(
    clients: Annotated[dict[str, AsyncClient], Depends(httpx_clients)],
    runtimes: Annotated[dict[str, OnnxChatRuntime], Depends(local_chat_runtimes)],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
    settings: Annotated[
        AgentSettings, Depends(session_agent_settings, scope="session")
    ],
) -> ChatProvider:
    """Чат-провайдер сессии: реализацию выбирает бэкенд профиля."""
    backend = settings.backend

    runtime = None
    if isinstance(backend, LocalChatConfig):
        runtime = runtimes[backend.model_dir]

    return ChatProviderFactory.build(
        backend,
        model=settings.model,
        client=clients.get(selected.name),
        runtime=runtime,
    )


def session_chat(
    provider: Annotated[ChatProvider, Depends(session_chat_provider, scope="session")],
    settings: Annotated[
        AgentSettings, Depends(session_agent_settings, scope="session")
    ],
) -> BaseChatModel:
    """Чат-модель хода: мост графа поверх провайдера с сэмплингом сессии."""
    return ProviderChatModel(
        provider=provider,
        sampling=settings.chat_sampling(),
        model_name=settings.model,
    )


def langchain_agent(
    chat: Annotated[BaseChatModel, Depends(session_chat, scope="session")],
    builder: Annotated[
        AgentGraphBuilder, Depends(session_graph_builder, scope="session")
    ],
    saver: Annotated[BaseCheckpointSaver, Depends(langchain_checkpoint_saver)],
    tools: Annotated[list[BaseTool], Depends(session_tools, scope="session")],
    settings: Annotated[
        AgentSettings, Depends(session_agent_settings, scope="session")
    ],
) -> CompiledStateGraph:
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

    return builder.build(spec)
