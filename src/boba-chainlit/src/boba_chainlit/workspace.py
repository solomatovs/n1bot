"""Workspace operations — типизированные результаты, валидация.

Workspace на диске идентифицируется UUID. Отображаемое имя — свойство в thread metadata.
Переименование = обновление metadata, без файловых операций.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from enum import Enum

import chainlit as cl
from chainlit.types import ThreadDict

from boba_domain.chat.thread import ThreadMetadata
from boba_domain.config import AppConfig
from boba_domain.core.thread_store import ChatThreadStore

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Typed errors
# ------------------------------------------------------------------


class ThreadMetadataError(Exception):
    """Метаданные thread'а из Chainlit отсутствуют или повреждены."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid thread metadata: {reason}")


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


@dataclass
class SessionState:
    folder: str
    display_name: str
    model: str


@dataclass(frozen=True)
class ParsedChatSettings:
    """Типизированные настройки чата из Chainlit ChatSettings widget."""

    model: str

    @classmethod
    def from_raw(cls, raw: dict, default_model: str) -> ParsedChatSettings:
        return cls(model=raw.get("model") or default_model)


def get_session_state() -> SessionState | None:
    """Typed state из Chainlit session. None если не инициализирован."""
    folder = cl.user_session.get("folder")
    display_name = cl.user_session.get("display_name")
    model = cl.user_session.get("model")
    if folder is None or display_name is None or model is None:
        return None
    return SessionState(folder=folder, display_name=display_name, model=model)


def set_session_state(state: SessionState) -> None:
    cl.user_session.set("folder", state.folder)
    cl.user_session.set("display_name", state.display_name)
    cl.user_session.set("model", state.model)


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


_DEFAULT_NAME_PREFIX = "workspace"


class WorkspaceService:
    """Операции над workspace. Папка на диске = UUID, имя = metadata."""

    def __init__(self, cfg: AppConfig, store: ChatThreadStore) -> None:
        self._cfg = cfg
        self._store = store

    def create(self, display_name: str | None = None) -> WorkspaceResult:
        """Создать workspace с UUID-папкой. Автоимя если display_name не задан."""
        folder_id = uuid.uuid4().hex
        self._cfg.workspace_path(folder_id).mkdir(parents=True, exist_ok=True)

        if not display_name:
            display_name = self._next_display_name()

        return WorkspaceResult(folder=folder_id, display_name=display_name)

    def validate_display_name(self, name: str) -> WorkspaceOutcome:
        """Валидация отображаемого имени. Нет файловых ограничений — только пустота."""
        name = name.strip()
        if not name:
            return WorkspaceFailure(WorkspaceError.INVALID_NAME)
        return WorkspaceResult(folder="", display_name=name)

    def _next_display_name(self) -> str:
        """Сгенерировать автоимя workspace-1, workspace-2, ..."""
        existing_names = {t.name for t in self._store.iter_threads()}
        n = 1
        while f"{_DEFAULT_NAME_PREFIX}-{n}" in existing_names:
            n += 1
        return f"{_DEFAULT_NAME_PREFIX}-{n}"
