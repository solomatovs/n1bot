"""DI-контейнер приложения."""

from __future__ import annotations

import logging
import sys

from dishka import make_container, Container

from boba_domain.config import AppConfig
from boba_infra.providers.app import AppProvider
from boba_infra.providers.agent import AgentProvider, ToolsProvider

_LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def create_container() -> Container:
    """Создать DI-контейнер приложения.

    Использование::

        container = create_container()

        # APP-scoped (singleton):
        cfg = container.get(AppConfig)

        # REQUEST-scoped (per folder):
        ctx = FolderContext(folder_path=path, history_path=history)
        with container(context={FolderContext: ctx}) as scope:
            agent = scope.get(AgentLoop)
            vs = scope.get(VectorStoreService)
    """
    container = make_container(AppProvider(), AgentProvider(), ToolsProvider())

    # Настроить логирование
    cfg = container.get(AppConfig)
    _configure_logging(cfg)

    return container


def _configure_logging(cfg: AppConfig) -> None:
    level = _LOG_LEVELS.get(cfg.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("watchdog").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("engineio.server").setLevel(logging.CRITICAL)
