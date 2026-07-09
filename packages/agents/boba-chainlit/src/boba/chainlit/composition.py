"""Composition root: сборка зависимостей и запуск chainlit-сервера."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from boba.agent.history import HistoryService, JsonLinesHistoryService
from boba.agent.tool_config import (
    OmegaConfPluginToolFilter,
    OmegaConfResolver,
    bind,
    build_app_config,
)
from boba.agent.workspace_fs import FsWorkspaceShell
from boba.chainlit.config import ChainlitConfig
from boba.chainlit.data_layer import BobaDataLayer
from boba.chainlit.logging import configure_logging
from boba.chainlit.models import ThreadId
from boba.chainlit.sessions import (
    ChatSession,
    ChatSessionPool,
    OpenChatSession,
)
from boba.chainlit.settings import (
    PromptSection,
    SettingsSection,
    ToolsSection,
)
from boba.chainlit.settings.model_section import ModelSection
from boba.chainlit.state import set_app_state
from boba.chainlit.storage import (
    FsThreadRepository,
    FsUserCatalog,
    ThreadRepository,
)
from boba.chainlit.system_prompt import DefaultSystemPromptSource
from boba.chainlit.tool_cache import AvailableToolsCache
from boba.chainlit2.chat.auth import PasswordAuthCallbackInstaller
from boba.chainlit2.infra.config import (
    FixAuthConfig,
    KerberosAuthConfig,
    LdapAuthConfig,
)
from boba.tools import ToolBuilder
from boba.workspace.contract import WorkspaceId

__all__ = ["main"]

_logger = logging.getLogger(__name__)

_SYSTEM_WORKSPACE_ID = WorkspaceId("_chainlit_system")
"""Workspace для системных файлов chainlit (users.json, threads-index.json)."""


def _bridge_chainlit_env(cfg: ChainlitConfig) -> None:
    """Прокидывает ChainlitConfig в CHAINLIT_* env.

    DTO — единственный источник истины: значения env жёстко перезаписываются,
    чтобы случайно унаследованный CHAINLIT_* из окружения не маскировал конфиг.
    """
    os.environ["CHAINLIT_HOST"] = cfg.host
    os.environ["CHAINLIT_PORT"] = str(cfg.port)
    os.environ["CHAINLIT_ROOT_PATH"] = cfg.url_prefix
    os.environ["CHAINLIT_AUTH_SECRET"] = cfg.auth_secret
    os.environ["CHAINLIT_HEADLESS"] = "true" if cfg.headless else "false"
    app_root = Path(cfg.app_root).resolve()
    app_root.mkdir(parents=True, exist_ok=True)
    os.environ["CHAINLIT_APP_ROOT"] = str(app_root)


def _make_tool_builder_factory(config: Any) -> Callable[[], ToolBuilder]:
    """Свежий ToolBuilder под per-session ChatSession (свои plugins).

    Резолвер/фильтр строятся вокруг переданного конфиг-инстанса (config
    собран один раз в main), поэтому per-session здесь — только ToolBuilder.

    discover_plugins() подцепляет v2-плагины через entry-points group
    boba.plugins. Пока v2-плагины не созданы — это no-op; будут
    появляться по мере миграции старых плагинов.
    """

    def factory() -> ToolBuilder:
        return (
            ToolBuilder()
            .use_config_resolver(OmegaConfResolver(config))
            .discover_plugins("boba.plugins", OmegaConfPluginToolFilter(config))
        )

    return factory


def _make_chat_session_builder(
    make_tool_builder: Callable[[], ToolBuilder],
    project_base_dir: Path,
    history_base_dir: Path,
    thread_repository: ThreadRepository,
    default_prompt_source: DefaultSystemPromptSource,
    available_tools: AvailableToolsCache,
) -> Callable[[WorkspaceId, ThreadId], ChatSession]:
    """Замыкание над deps; возвращает фабрику ChatSession по (workspace, thread)."""

    def build(workspace_id: WorkspaceId, thread_id: ThreadId) -> ChatSession:
        return ChatSession(
            workspace_id,
            thread_id,
            make_tool_builder(),
            project_base_dir,
            history_base_dir,
            thread_repository,
            default_prompt_source,
            available_tools,
        )

    return build


def _warm_tool_cache(
    chat_session_builder: Callable[[WorkspaceId, ThreadId], ChatSession],
) -> None:
    """Сборкой одноразовой ChatSession наполняем AvailableToolsCache.

    ChatSession.__init__ строит catalog и через _wrap_catalog
    зовёт set_once; повторный set_once на реальной сессии — no-op
    (контракт set-once). Если сборка падает — лог + продолжаем: tools
    потом подтянутся при первом on_message.
    """
    warm_thread = ThreadId(f"warmup-{uuid.uuid4()}")
    try:
        chat_session_builder(_SYSTEM_WORKSPACE_ID, warm_thread)
    except Exception:
        _logger.exception(
            "warmup ChatSession failed; tool checkboxes will appear after "
            "the first user message",
        )


def main() -> int:
    # получаем настройки всего приложения
    if (config_path := os.environ.get("BOBA_CONFIG_PATH")) is None:
        raise ValueError("please pass env BOBA_CONFIG_PATH")

    config = build_app_config(config_path=Path(config_path))
    chainlit_cfg = bind(config, "chainlit", ChainlitConfig)
    rt = chainlit_cfg.profile

    configure_logging(rt.log_level, rt.log_file)

    _bridge_chainlit_env(chainlit_cfg)

    project_base_dir = Path(rt.user_workspace_dir)
    history_base_dir = Path(rt.system_workspace_dir)

    def make_history_service(workspace_id: WorkspaceId) -> HistoryService:
        """JSONL per-workspace: chainlit и агент видят один и тот же журнал."""
        return JsonLinesHistoryService(
            FsWorkspaceShell.under(history_base_dir, workspace_id)
        )

    # Свежий shell на каждую службу: общий root, но изолированное состояние.
    user_catalog = FsUserCatalog(
        FsWorkspaceShell.under(history_base_dir, _SYSTEM_WORKSPACE_ID)
    )
    thread_repository = FsThreadRepository(
        FsWorkspaceShell.under(history_base_dir, _SYSTEM_WORKSPACE_ID)
    )
    default_prompt_source = DefaultSystemPromptSource(Path(rt.system_prompt_dir))
    available_tools = AvailableToolsCache()

    tool_builder_factory = _make_tool_builder_factory(config)
    chat_session_builder = _make_chat_session_builder(
        tool_builder_factory,
        project_base_dir,
        history_base_dir,
        thread_repository,
        default_prompt_source,
        available_tools,
    )
    chat_session_pool = ChatSessionPool(
        chat_session_builder,
        capacity=chainlit_cfg.chat_session_pool_capacity,
    )
    open_chat_session = OpenChatSession(chat_session_pool)

    # Eager-прогрев AvailableToolsCache: до первого on_chat_start cache пуст
    # -> ToolsSection отдаёт пустой список -> в шестерёнке нет таба «tools».
    # Одноразовая ChatSession через system workspace выполняет
    # ToolBuilder.discover_plugins + сборку catalog'а, что через
    # _wrap_catalog триггерит set_once. Сессия и её агент не
    # используются и сразу gc'ятся; workspace dir тот же, что под
    # users.json / threads-index.json, новых каталогов на диске не появляется.
    _warm_tool_cache(chat_session_builder)

    data_layer = BobaDataLayer(
        user_catalog,
        thread_repository,
        make_history_service,
    )

    # Реестр секций шестерёнки. Порядок = порядок табов слева направо.
    sections: tuple[SettingsSection, ...] = (
        ModelSection(rt.models),
        PromptSection(default_prompt_source),
        ToolsSection(available_tools),
    )

    set_app_state(
        open_chat_session,
        data_layer,
        thread_repository,
        default_prompt_source,
        available_tools,
        sections,
    )

    # Монтируем chainlit в FastAPI (вместо run_chainlit), чтобы авторизация
    # из boba-chainlit2 (middleware + password/header callback'и) и доменные
    # ошибки подключались тем же способом, что и в новом приложении.
    app = FastAPI()
    _use_chainlit_app(app, chainlit_cfg)
    _use_auth(chainlit_cfg)
    _use_domain_error(app)

    asyncio.run(_serve(app, chainlit_cfg))
    return 0


def _use_chainlit_app(app: FastAPI, cfg: ChainlitConfig) -> None:
    """Монтирует chainlit-приложение в FastAPI (замена run_chainlit)."""
    # импорт callback-модуля регистрирует cl.* хендлеры (как делал load_module
    # внутри run_chainlit); chainlit уже импортирован выше через data_layer.
    import boba.chainlit.callbacks  # type: ignore  # noqa: F401, PLC0415
    from chainlit.config import config as cl_config  # noqa: PLC0415
    from chainlit.markdown import init_markdown  # noqa: PLC0415
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    # то, что run_chainlit проставлял из env, выставляем явно из конфига
    cl_config.run.host = cfg.host
    cl_config.run.port = cfg.port
    cl_config.run.root_path = cfg.url_prefix

    # chainlit.md, если его ещё нет
    init_markdown(cl_config.root)

    class ChainlitMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            if not request.url.path.startswith(cfg.url_prefix):
                return JSONResponse(status_code=404, content={"detail": "Not found"})
            return await call_next(request)

    if cfg.url_prefix:
        chainlit_app.add_middleware(ChainlitMiddleware)

    app.mount(cfg.url_prefix, chainlit_app)


def _use_auth(config: ChainlitConfig) -> None:
    """Единая точка подключения авторизации; стратегия выбирается конфигом."""
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    password_callback = PasswordAuthCallbackInstaller()

    for auth in config.auth:
        if isinstance(auth, KerberosAuthConfig):
            from boba.chainlit2.chat.auth.kerberos import (  # noqa: PLC0415
                KerberosAuth,
            )

            KerberosAuth(config.url_prefix, auth).install(chainlit_app)

        elif isinstance(auth, FixAuthConfig):
            from boba.chainlit2.chat.auth.fix import (  # noqa: PLC0415
                FixAuth,
            )

            password_callback.fix_auth_setup(FixAuth(auth))

        elif isinstance(auth, LdapAuthConfig):
            from boba.chainlit2.chat.auth.ldap import (  # noqa: PLC0415
                LdapAuth,
            )

            password_callback.ldap_auth_setup(LdapAuth(auth))

        else:
            raise ValueError(f"unknown authorization type: {type(auth).__name__}")

    # устанавливаю password callback если есть хотя бы один из вариантов
    password_callback.install_callback_if_any_exists()


def _use_domain_error(app: FastAPI) -> None:
    """Единая точка обработки доменных ошибок (вкл. SPNEGO-челлендж → 401)."""
    from boba.chainlit2.errors import DomainErrorMiddleware  # noqa: PLC0415
    from chainlit.server import app as chainlit_app  # noqa: PLC0415

    app.add_middleware(DomainErrorMiddleware)
    chainlit_app.add_middleware(DomainErrorMiddleware)


async def _serve(app: FastAPI, cfg: ChainlitConfig) -> None:
    """Запуск uvicorn на внешнем FastAPI (chainlit смонтирован внутрь)."""
    uv_config = uvicorn.Config(
        app,
        host=cfg.host,
        port=cfg.port,
        # log_config=None — не перетираем уже применённый configure_logging
        log_config=None,
        access_log=True,
    )
    server = uvicorn.Server(uv_config)
    await server.serve()
