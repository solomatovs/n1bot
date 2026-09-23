"""Провайдеры chainlit-процесса: конфиг чата, клиенты LLM, data layer и агент langgraph.

Общие для процессов провайдеры (реестр, сторы, workflow) — boba.runtime.providers.
"""

from collections.abc import AsyncIterator, Sequence
from typing import Annotated

from langchain.agents.middleware import ModelRequest, wrap_model_call
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph

from boba.auth import JwtTokens
from boba.chainlit.agent.bridge import ChatModelBridge
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
from boba.chainlit.data import PostgresDataLayer
from boba.chainlit.data.storage import StorageClient, StorageFactory
from boba.chainlit.domain.keys import AttachmentLinks
from boba.chainlit.infra.config import (
    AppConfig,
    CheckpointerConfig,
    DataLayerConfig,
    LocalStorageConfig,
)
from boba.chainlit.infra.session import ChainlitSessions, current_session
from boba.chainlit.rendering.chat_view import StepText
from boba.chat.profiles import (
    AgentSettings,
    ChatProfiles,
    PrefetchFlowConfig,
    SelectedProfile,
    SettingsView,
    UserMeta,
)
from boba.db.postgres import AsyncPostgresPool, PostgresError, PostgresSchema
from boba.identity.errors import InternalServiceError
from boba.identity.session import SessionSource
from boba.llm.providers import LlmProviders, LlmProviderTypes
from boba.llm.schema import SchemaReply
from boba.messaging import MessageBus
from boba.runtime import providers as runtime
from boba.runtime.di import Depends
from boba.runtime.elements import ChatTables
from boba.runtime.users import UsersTable
from boba.toolrun.registry import ToolRegistry


def get_app_config() -> AppConfig:
    """Конфиг chainlit-процесса; значение кладёт bootstrap после AppConfig.load."""
    msg = (
        "get_app_config: the app config is set by bootstrap as a container "
        "override, the provider itself produces nothing"
    )
    raise RuntimeError(msg)


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

    return registry.resolve(session.chat_profile, session.sign_in)


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


def session_source(
    tokens: Annotated[JwtTokens, Depends(runtime.session_tokens)],
) -> SessionSource:
    """Источник сессий приложения; реализация знает про chainlit."""
    return ChainlitSessions(tokens)


def session_tools(
    registry: Annotated[ToolRegistry, Depends(runtime.tool_registry)],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
) -> list[BaseTool]:
    return registry.for_session(current_session().roles, selected.name)


async def llm_providers(
    c: Annotated[AppConfig, Depends(get_app_config)],
) -> AsyncIterator[LlmProviders]:
    """Модели процесса: бэкенды всех профилей собираются на старте, чтобы
    первая сессия не ждала весов локальной модели; закрываются на остановке."""
    providers = LlmProviders(LlmProviderTypes.installed())
    for profile in c.profiles.values():
        providers.chat(profile)

        flow = profile.flow
        if not isinstance(flow, PrefetchFlowConfig):
            continue

        if flow.rephraser is None:
            continue

        providers.chat(flow.rephraser)

    try:
        yield providers
    finally:
        await providers.aclose()


async def langchain_checkpoint_saver(
    cp: Annotated[CheckpointerConfig, Depends(get_checkpointer_config)],
) -> BaseCheckpointSaver:
    """Савер langgraph на пуле со своим search_path: схему в имена он не ставит.

    Пул закрывает остановка приложения, а не провайдер: пул общий для всех, кто
    попросит его с той же схемой.
    """
    pool = await AsyncPostgresPool.get(cp.postgres.with_schema(cp.db_schema))
    try:
        await PostgresSchema(cp.db_schema).ensure_with(pool)
    except PostgresError as e:
        raise InternalServiceError(
            internal_detail=(
                f"checkpointer: ensuring postgres schema {cp.db_schema!r} failed: {e}"
            ),
            user_detail="Failed to connect to the internal postgres",
        ) from e

    saver = AsyncPostgresSaver(pool.raw)
    await saver.setup()

    return saver


async def chainlit_data_layer(  # noqa: PLR0913 — слой данных собирается всеми зависимостями сразу
    cfg: Annotated[DataLayerConfig, Depends(get_data_layer_config)],
    storage_cfg: Annotated[LocalStorageConfig, Depends(get_local_storage_config)],
    storage: Annotated[StorageClient, Depends(storage_provider)],
    saver: Annotated[BaseCheckpointSaver, Depends(langchain_checkpoint_saver)],
    bus: Annotated[MessageBus, Depends(runtime.message_bus)],
    users: Annotated[UsersTable, Depends(runtime.users_table)],
    sessions: Annotated[SessionSource, Depends(session_source)],
) -> PostgresDataLayer:
    """Слой данных чата на общем пуле процесса: схему таблицы ставят в запрос."""
    pool = await AsyncPostgresPool.get(cfg.postgres)
    tables = ChatTables.around(users, cfg.postgres, cfg.db_schema, pool)
    await tables.setup()

    return PostgresDataLayer(
        users=tables.users,
        threads=tables.threads,
        elements=tables.elements,
        feedbacks=tables.feedbacks,
        storage=storage,
        feed=TranscriptFeed(CheckpointMessages(saver)),
        links=AttachmentLinks(storage_cfg.public_prefix),
        sessions=sessions,
        bus=bus,
    )


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
            available = ", ".join(sorted(by_name))
            msg = (
                f"flow tool {name!r} is not available to the session; "
                f"available tools: {available}"
            )
            raise RuntimeError(msg)

        selected.append(found)

    return selected


def session_graph_builder(
    providers: Annotated[LlmProviders, Depends(llm_providers)],
    selected: Annotated[SelectedProfile, Depends(session_profile, scope="session")],
    tools: Annotated[Sequence[BaseTool], Depends(session_tools, scope="session")],
) -> AgentGraphBuilder:
    """Билдер графа хода по flow профиля."""
    flow = selected.config.flow
    if not isinstance(flow, PrefetchFlowConfig):
        return PlainGraphBuilder()

    rephraser = _rephraser(providers, flow)
    stage = TracedStage(StepText.PREFETCH.value)
    return PrefetchGraphBuilder(rephraser, _flow_tools(flow.tools, tools), stage)


def _rephraser(providers: LlmProviders, flow: PrefetchFlowConfig) -> Rephraser:
    """Модель, готовящая поисковые запросы; без секции — запрос идёт как есть."""
    cfg = flow.rephraser
    if cfg is None:
        return PassthroughRephraser()

    reply = SchemaReply(providers.chat(cfg), cfg.sampling)

    return LlmRephraser(reply, cfg.system_prompt)


def session_chat(
    providers: Annotated[LlmProviders, Depends(llm_providers)],
    settings: Annotated[
        AgentSettings, Depends(session_agent_settings, scope="session")
    ],
) -> BaseChatModel:
    """Чат-модель хода: мост графа поверх модели профиля с сэмплингом сессии."""
    return ChatModelBridge(
        chat_model=providers.chat(settings),
        sampling=dict(settings.sampling),
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
