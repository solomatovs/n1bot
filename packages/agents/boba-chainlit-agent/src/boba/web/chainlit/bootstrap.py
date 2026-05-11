"""Bootstrap-канал между `__main__.main()` и Chainlit-callback'ами в `app.py`.

Chainlit владеет жизненным циклом: импортирует `app.py` уже после `main()`,
зовёт `on_chat_start` сам. Прямого способа прокинуть deps в `ChatSession`
конструктор у нас нет — нужен явный shared-state канал.

Изолируем его в этом модуле, а не на классах: `ChatSession` остаётся
чистым DTO-приёмником через конструктор.
"""

from __future__ import annotations

from dataclasses import dataclass

from boba.agent.builder import AgentBuilder
from boba.workspace.contract import (
    HistoryWorkspaceRegistry,
    ProjectWorkspaceRegistry,
)

__all__ = ["AppState", "app_state", "set_app_state"]


@dataclass(frozen=True)
class AppState:
    """Application-wide deps, зафиксированные в `main()`."""

    builder: AgentBuilder
    project_workspaces: ProjectWorkspaceRegistry
    history_workspaces: HistoryWorkspaceRegistry


@dataclass
class _Holder:
    """Контейнер с одним слотом: мутируется через атрибут, без `global`-statement."""

    state: AppState | None = None


_holder = _Holder()


def set_app_state(
    builder: AgentBuilder,
    project_workspaces: ProjectWorkspaceRegistry,
    history_workspaces: HistoryWorkspaceRegistry,
) -> None:
    """Зафиксировать deps до `run_chainlit(...)`."""
    _holder.state = AppState(
        builder=builder,
        project_workspaces=project_workspaces,
        history_workspaces=history_workspaces,
    )


def app_state() -> AppState:
    """Прочитать deps из `app.py`-callback'ов."""
    if _holder.state is None:
        msg = "bootstrap.set_app_state(...) не вызван до run_chainlit(...)"
        raise RuntimeError(msg)
    return _holder.state
