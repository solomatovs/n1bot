"""Bootstrap-канал между `__main__.main()` и Chainlit-callback'ами в `app.py`.

Chainlit владеет жизненным циклом: импортирует `app.py` уже после `main()`,
зовёт callback'и сам. Прямого способа прокинуть deps нет — нужен явный
shared-state канал. Этот модуль и есть канал; AppState содержит только то,
что нужно Chainlit-слою (use cases, авторизация, data_layer, ownership).
"""

from __future__ import annotations

from dataclasses import dataclass

from boba.web.chainlit.auth import AuthenticateUser
from boba.web.chainlit.data_layer import BobaDataLayer, WorkspaceOwnership
from boba.web.chainlit.usecase import OpenChatSession

__all__ = ["AppState", "app_state", "set_app_state"]


@dataclass(frozen=True)
class AppState:
    """Application-wide deps, зафиксированные в `main()`."""

    authenticate_user: AuthenticateUser
    open_chat_session: OpenChatSession
    data_layer: BobaDataLayer
    workspace_ownership: WorkspaceOwnership


@dataclass
class _Holder:
    """Контейнер с одним слотом: мутируется через атрибут, без `global`-statement."""

    state: AppState | None = None


_holder = _Holder()


def set_app_state(
    authenticate_user: AuthenticateUser,
    open_chat_session: OpenChatSession,
    data_layer: BobaDataLayer,
    workspace_ownership: WorkspaceOwnership,
) -> None:
    """Зафиксировать deps до `run_chainlit(...)`."""
    _holder.state = AppState(
        authenticate_user=authenticate_user,
        open_chat_session=open_chat_session,
        data_layer=data_layer,
        workspace_ownership=workspace_ownership,
    )


def app_state() -> AppState:
    """Прочитать deps из `app.py`-callback'ов."""
    if _holder.state is None:
        msg = "bootstrap.set_app_state(...) не вызван до run_chainlit(...)"
        raise RuntimeError(msg)

    return _holder.state
