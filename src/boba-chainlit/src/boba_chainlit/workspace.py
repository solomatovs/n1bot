"""Workspace operations — типизированные результаты, валидация.

Workspace на диске идентифицируется UUID. Отображаемое имя — свойство в thread metadata.
Переименование = обновление metadata, без файловых операций.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum

import chainlit as cl
from chainlit.types import ThreadDict

from boba_domain.chat.thread import ThreadMetadata
from boba_domain.config import AppConfig
from boba_domain.core.thread_store import ChatThreadStore
from boba_domain.errors import ThreadNotFoundError

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Typed errors
# ------------------------------------------------------------------


class ThreadMetadataError(Exception):
    """Метаданные thread'а из Chainlit отсутствуют или повреждены."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid thread metadata: {reason}")


class NoAuthenticatedUserError(Exception):
    """В Chainlit-сессии нет авторизованного пользователя."""

    def __init__(self) -> None:
        super().__init__(
            "No authenticated user in Chainlit session. "
            "Убедитесь что @cl.header_auth_callback зарегистрирован."
        )


class DataLayerNotInitializedError(Exception):
    """Chainlit data layer не инициализирован."""

    def __init__(self) -> None:
        super().__init__(
            "Chainlit data layer is None. "
            "Убедитесь что @cl.data_layer зарегистрирован."
        )


class SessionNotInitializedError(Exception):
    """SessionState ещё не создан в текущей Chainlit-сессии."""

    def __init__(self, missing_fields: list[str]) -> None:
        self.missing_fields = missing_fields
        fields = ", ".join(missing_fields)
        super().__init__(
            f"SessionState not initialized: missing fields [{fields}]. "
            "on_chat_start или on_chat_resume должен быть вызван до обращения к сессии."
        )


class WorkspaceError(Enum):
    INVALID_NAME = "Недопустимое имя пространства"
    NOT_FOUND = "Пространство не найдено"
    CORRUPT_METADATA = "Повреждённые метаданные чата"


@dataclass(frozen=True)
class WorkspaceFailure:
    error: WorkspaceError
    detail: str = ""


@dataclass(frozen=True)
class WorkspaceResult:
    folder: str
    display_name: str


WorkspaceOutcome = WorkspaceResult | WorkspaceFailure


# ------------------------------------------------------------------
# Session state
# ------------------------------------------------------------------

_DEFAULT_MAX_TOKENS = 8192


@dataclass
class SessionState:
    folder: str
    display_name: str
    model: str
    user_id: str
    max_tokens: int
    tags: list[str] = field(default_factory=list)

    def to_thread_metadata(self) -> dict:
        """Метаданные для сохранения в thread.json через data layer."""
        return {"folder": self.folder, "model": self.model}


@dataclass(frozen=True)
class ParsedChatSettings:
    """Типизированные настройки чата из Chainlit ChatSettings widget."""

    model: str
    max_tokens: int = _DEFAULT_MAX_TOKENS

    @classmethod
    def from_raw(cls, raw: dict, default_model: str) -> ParsedChatSettings:
        return cls(
            model=raw.get("model") or default_model,
            max_tokens=int(raw.get("max_tokens") or _DEFAULT_MAX_TOKENS),
        )


def get_current_user_id() -> str:
    """Идентификатор текущего пользователя из Chainlit-сессии."""
    user = cl.context.session.user
    if user is None:
        raise NoAuthenticatedUserError()
    
    return user.identifier


_SESSION_STATE_KEY = "session_state"


def get_session_state() -> SessionState:
    """Получить SessionState из Chainlit session. Бросает SessionNotInitializedError."""
    state = cl.user_session.get(_SESSION_STATE_KEY)
    
    if not isinstance(state, SessionState):
        raise SessionNotInitializedError(["SessionState"])
    
    return state


def set_session_state(state: SessionState) -> None:
    """Сохранить SessionState в Chainlit session."""
    cl.user_session.set(_SESSION_STATE_KEY, state)


# ------------------------------------------------------------------
# Error renderer
# ------------------------------------------------------------------


async def send_error(failure: WorkspaceFailure) -> None:
    """Отобразить типизированную ошибку в чат."""
    text = failure.error.value
    if failure.detail:
        text += f": **{failure.detail}**"
    await cl.Message(content=text).send()


# ------------------------------------------------------------------
# Metadata parsing
# ------------------------------------------------------------------


def parse_thread_metadata(thread: ThreadDict) -> ThreadMetadata:
    """Парсинг metadata из ThreadDict. Бросает ThreadMetadataError."""
    raw = thread.get("metadata")
    
    if raw is None:
        raise ThreadMetadataError("metadata отсутствует")
    
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise ThreadMetadataError(f"невалидный JSON: {raw[:100]}")
    
    if not isinstance(raw, dict):
        raise ThreadMetadataError(f"ожидался dict, получен {type(raw).__name__}")
    
    if "folder" not in raw:
        raise ThreadMetadataError("отсутствует обязательное поле 'folder'")
    
    return ThreadMetadata(
        folder=raw["folder"],
        model=raw.get("model", ""),
    )


# ------------------------------------------------------------------
# Workspace service
# ------------------------------------------------------------------

class WorkspaceService:
    """Операции над workspace. Папка на диске = UUID, имя = metadata."""

    def __init__(self, cfg: AppConfig, store: ChatThreadStore) -> None:
        self._cfg = cfg
        self._store = store
        self._DEFAULT_NAME_PREFIX = "workspace"

    def ensure(self, thread_id: str) -> WorkspaceResult:
        """Вернуть существующий workspace или создать новый. Идемпотентно."""
        try:
            thread = self._store.get_thread(thread_id)
            return WorkspaceResult(folder=thread.metadata.folder, display_name=thread.name)
        except ThreadNotFoundError:
            return self._create(thread_id)

    def _create(self, thread_id: str) -> WorkspaceResult:
        """Создать новый workspace на диске."""
        self._cfg.workspace_path(thread_id).mkdir(parents=True, exist_ok=True)
        display_name = self._gen_next_display_name()
        return WorkspaceResult(folder=thread_id, display_name=display_name)

    def validate_display_name(self, name: str) -> WorkspaceOutcome:
        """Валидация отображаемого имени. Нет файловых ограничений — только пустота."""
        name = name.strip()
        if not name:
            return WorkspaceFailure(WorkspaceError.INVALID_NAME)
        return WorkspaceResult(folder="", display_name=name)

    def _gen_next_display_name(self) -> str:
        """Сгенерировать автоимя workspace-N (N = количество чатов + 1)."""
        count = sum(1 for _ in self._store.iter_threads())
        return f"{self._DEFAULT_NAME_PREFIX}-{count + 1}"
