"""Workspace operations — типизированные результаты, валидация, session registry.

Workspace на диске идентифицируется UUID (FolderId).
ThreadId — идентификатор Chainlit-сессии, не протекает в domain.
SessionRegistry — in-memory маппинг ThreadId → FolderId.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum

import chainlit as cl
from chainlit.types import ThreadDict

from boba_domain.chat.thread import ThreadMetadata
from boba_domain.config import AppConfig
from boba_domain.core.thread_store import ChatThreadStore

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Typed identifiers
# ------------------------------------------------------------------

@dataclass(frozen=True)
class ThreadId:
    """Идентификатор Chainlit-сессии. Не протекает в domain."""

    value: str


@dataclass(frozen=True)
class FolderId:
    """Идентификатор workspace на диске (UUID-имя папки)."""

    value: str


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


_DEFAULT_MODEL = ""
_DEFAULT_MAX_TOKENS = 8192


@dataclass(frozen=True)
class WorkspaceSettings:
    folder: FolderId
    display_name: str
    model: str = _DEFAULT_MODEL
    max_tokens: int = _DEFAULT_MAX_TOKENS


WorkspaceOutcome = WorkspaceSettings | WorkspaceFailure


# ------------------------------------------------------------------
# Session state
# ------------------------------------------------------------------


@dataclass
class SessionState:
    folder: FolderId
    display_name: str
    model: str
    user_id: str
    max_tokens: int
    tags: list[str] = field(default_factory=list)

    def to_thread_metadata(self) -> dict:
        """Метаданные для сохранения в thread.json через data layer."""
        return {"folder": self.folder.value, "model": self.model}


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
# Session registry
# ------------------------------------------------------------------


class SessionRegistry:
    """In-memory маппинг ThreadId → FolderId.

    Живёт только в памяти Chainlit-процесса.
    ThreadId — деталь Chainlit-транспорта, не протекает в domain.
    """

    def __init__(self) -> None:
        self._sessions: dict[ThreadId, FolderId] = {}

    def bind(self, thread_id: ThreadId, folder: FolderId) -> None:
        """Привязать Chainlit thread к workspace."""
        self._sessions[thread_id] = folder

    def get_folder(self, thread_id: ThreadId) -> FolderId | None:
        """Найти folder по thread_id. O(1)."""
        return self._sessions.get(thread_id)

    def unbind(self, thread_id: ThreadId) -> None:
        """Удалить привязку."""
        self._sessions.pop(thread_id, None)


# ------------------------------------------------------------------
# Workspace service
# ------------------------------------------------------------------


class WorkspaceService:
    """Операции над workspace. Folder на диске = UUID."""

    _DEFAULT_NAME_PREFIX = "workspace"

    def __init__(self, cfg: AppConfig, store: ChatThreadStore) -> None:
        self._cfg = cfg
        self._store = store

    def ensure_or_first(self) -> WorkspaceSettings:
        """Вернуть первый существующий workspace или создать новый.

        Единственная точка создания workspace на диске.
        """
        first = next(self._store.iter_threads(), None)
        if first is not None:
            folder = FolderId(first.metadata.folder)
            self._ensure_dirs(folder)
            return WorkspaceSettings(
                folder=folder,
                display_name=first.name,
                model=first.metadata.model,
            )
        return self._create()

    def get(self, folder: FolderId) -> WorkspaceSettings:
        """Получить настройки существующего workspace по folder."""
        thread = self._store.get_thread_by_folder(folder.value)
        return WorkspaceSettings(
            folder=folder,
            display_name=thread.name,
            model=thread.metadata.model,
        )

    def _create(self) -> WorkspaceSettings:
        """Создать новый workspace с UUID-именем."""
        folder = FolderId(str(uuid.uuid4()))
        self._ensure_dirs(folder)
        display_name = self._gen_next_display_name()
        return WorkspaceSettings(folder=folder, display_name=display_name)

    def _ensure_dirs(self, folder: FolderId) -> None:
        """Создать структуру workspace на диске (идемпотентно)."""
        workspace_path = self._cfg.workspace_path(folder.value)
        workspace_path.mkdir(parents=True, exist_ok=True)
        self._cfg.boba_path(workspace_path).mkdir(parents=True, exist_ok=True)
        self._cfg.workspace_history_path(workspace_path).touch(exist_ok=True)

    def validate_display_name(self, name: str) -> WorkspaceOutcome:
        """Валидация отображаемого имени. Нет файловых ограничений — только пустота."""
        name = name.strip()
        if not name:
            return WorkspaceFailure(WorkspaceError.INVALID_NAME)
        return WorkspaceSettings(folder=FolderId(""), display_name=name)

    def _gen_next_display_name(self) -> str:
        """Сгенерировать автоимя workspace-N (N = количество чатов + 1)."""
        count = sum(1 for _ in self._store.iter_threads())
        return f"{self._DEFAULT_NAME_PREFIX}-{count + 1}"
