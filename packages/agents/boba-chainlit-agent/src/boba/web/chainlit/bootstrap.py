"""Bootstrap-канал между `__main__.main()` и Chainlit-callback'ами в `app.py`.

Chainlit владеет жизненным циклом: импортирует `app.py` уже после `main()`,
зовёт `on_chat_start` сам. Прямого способа прокинуть deps в `ChatSession`
конструктор у нас нет — нужен явный shared-state канал.

Изолируем его в этом модуле, а не на классах: `ChatSession` остаётся
чистым DTO-приёмником через конструктор.

Хранится **фабрика** AgentBuilder'ов: каждая сессия привязана к своему
workspace_id и регистрирует ProjectWorkspaceShell на свежем builder'е,
поэтому шарить один builder между сессиями нельзя.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from boba.agent.builder import AgentBuilder
from boba.agent.messages import MessageService
from boba.workspace.catalog import WorkspaceCatalog
from boba.workspace.contract import (
    HistoryWorkspaceRegistry,
    ProjectWorkspaceRegistry,
    WorkspaceId,
)

__all__ = ["AppState", "app_state", "set_app_state"]

BuilderFactory = Callable[[], AgentBuilder]
MessageServiceFactory = Callable[[WorkspaceId], MessageService]


@dataclass(frozen=True)
class AppState:
    """Application-wide deps, зафиксированные в `main()`."""

    make_builder: BuilderFactory
    project_workspaces: ProjectWorkspaceRegistry
    history_workspaces: HistoryWorkspaceRegistry
    catalog: WorkspaceCatalog
    make_message_service: MessageServiceFactory


@dataclass
class _Holder:
    """Контейнер с одним слотом: мутируется через атрибут, без `global`-statement."""

    state: AppState | None = None


_holder = _Holder()


def set_app_state(
    make_builder: BuilderFactory,
    project_workspaces: ProjectWorkspaceRegistry,
    history_workspaces: HistoryWorkspaceRegistry,
    catalog: WorkspaceCatalog,
    make_message_service: MessageServiceFactory,
) -> None:
    """Зафиксировать deps до `run_chainlit(...)`."""
    _holder.state = AppState(
        make_builder=make_builder,
        project_workspaces=project_workspaces,
        history_workspaces=history_workspaces,
        catalog=catalog,
        make_message_service=make_message_service,
    )


def app_state() -> AppState:
    """Прочитать deps из `app.py`-callback'ов."""
    if _holder.state is None:
        msg = "bootstrap.set_app_state(...) не вызван до run_chainlit(...)"
        raise RuntimeError(msg)
    return _holder.state
