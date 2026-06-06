"""Application-state канал между `composition.main()` и Chainlit-callback'ами.

Chainlit владеет жизненным циклом: импортирует callback-модуль уже после
`main()`, зовёт callback'и сам. Прямого способа прокинуть deps нет —
нужен явный shared-state канал. Этот модуль и есть канал.
"""

from __future__ import annotations

from dataclasses import dataclass

from boba.chainlit.auth import AuthenticateUser
from boba.chainlit.data_layer import BobaDataLayer
from boba.chainlit.sessions import OpenChatSession
from boba.chainlit.settings import SettingsSection
from boba.chainlit.storage import ThreadRepository
from boba.chainlit.system_prompt import DefaultSystemPromptSource
from boba.chainlit.tool_cache import AvailableToolsCache

__all__ = ["AppState", "app_state", "set_app_state"]


@dataclass(frozen=True)
class AppState:
    """Application-wide deps, зафиксированные в `main()`."""

    authenticate_user: AuthenticateUser
    open_chat_session: OpenChatSession
    data_layer: BobaDataLayer
    thread_repository: ThreadRepository
    default_prompt_source: DefaultSystemPromptSource
    available_tools: AvailableToolsCache
    sections: tuple[SettingsSection, ...]


@dataclass
class _Holder:
    """Контейнер с одним слотом: мутируется через атрибут, без `global`-statement."""

    state: AppState | None = None


_holder = _Holder()


def set_app_state(
    authenticate_user: AuthenticateUser,
    open_chat_session: OpenChatSession,
    data_layer: BobaDataLayer,
    thread_repository: ThreadRepository,
    default_prompt_source: DefaultSystemPromptSource,
    available_tools: AvailableToolsCache,
    sections: tuple[SettingsSection, ...],
) -> None:
    """Зафиксировать deps до `run_chainlit(...)`."""
    _holder.state = AppState(
        authenticate_user=authenticate_user,
        open_chat_session=open_chat_session,
        data_layer=data_layer,
        thread_repository=thread_repository,
        default_prompt_source=default_prompt_source,
        available_tools=available_tools,
        sections=sections,
    )


def app_state() -> AppState:
    """Прочитать deps из chainlit-callback'ов."""
    if _holder.state is None:
        msg = "state.set_app_state(...) не вызван до run_chainlit(...)"
        raise RuntimeError(msg)

    return _holder.state
