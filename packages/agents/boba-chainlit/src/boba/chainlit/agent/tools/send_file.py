"""Tool send_file: отправляет файл из workspace вложением в чат.

Ошибки: ErrorResult — нет сессии, нет живого хода, путь вне каталога
вложений треда, файла нет или хранилище его не отдало; остальное
упаковывает ToolErrorGuard.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, ClassVar

from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from pydantic import Field

import chainlit as cl
from boba.chainlit.data.data_layer import AttachmentDataLayer
from boba.chainlit.data.storage import StorageError, StorageNotFoundError
from boba.chainlit.domain.errors import RefusalError
from boba.chainlit.domain.keys import ElementProps, ObjectKey
from boba.chainlit.domain.session import RequiredSession
from boba.chainlit.domain.turn import TurnContext
from boba.chainlit.rendering.chat_view import ChatView, StepRole
from boba.toolkit.result import ErrorResult, TextResult, ToolResult, pack_result
from chainlit.data import get_data_layer

__all__ = [
    "AttachmentErrorKind",
    "AttachmentRefusedError",
    "FileAttachment",
    "WorkspaceFile",
    "build_send_file_tool",
]


class AttachmentErrorKind(StrEnum):
    """Коды отказов send_file: уезжают в ErrorResult.error_kind."""

    NO_TURN = "no_turn"
    NO_TOOL_CALL = "no_tool_call"
    BAD_PATH = "bad_path"
    FILE_NOT_FOUND = "file_not_found"
    STORAGE_ERROR = "storage_error"


class AttachmentRefusedError(RefusalError):
    """Файл отправить нельзя; текст причины готов для LLM."""


@dataclass
class WorkspaceFile(cl.File):
    """Файл workspace: помимо штатных полей несёт каталог в props.

    Ссылка на вложение вычисляется при чтении треда, поэтому каталог обязан
    храниться рядом с элементом — иначе отдача ищет файл только в upload/.
    """

    props: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AttachmentTarget:
    """Разобранный запрос: файл, элемент и шаг, к которому он крепится."""

    key: ObjectKey
    element_id: str
    for_id: str


class FileAttachment:
    """Отправка файла из workspace штатным элементом chainlit."""

    PATH_DESCRIPTION: ClassVar[str] = (
        "Путь к файлу в каталогах треда: '/workspace/<thread_id>/upload/<имя>' "
        "или '/workspace/<thread_id>/mermaid/<имя>'. Файл из другого места "
        "workspace сначала перенеси туда через bash."
    )

    FALLBACK_MIME: ClassVar[str] = "application/octet-stream"

    @classmethod
    async def attach(cls, path: str, tool_call_id: str) -> ToolResult:
        try:
            target = cls._resolve(path, tool_call_id)
            await cls._require_file(target.key)
        except RefusalError as e:
            return ErrorResult(message=str(e), error_kind=e.kind)

        await cls._send(target)
        return TextResult(
            text=f"file attached to the chat: {target.key.name}",
            metadata={"path": target.key.in_workspace()},
        )

    @classmethod
    async def _require_file(cls, key: ObjectKey) -> None:
        """Ссылка на несуществующий файл открылась бы у пользователя как 404."""
        try:
            await cls._layer().storage.stat(key.render())
        except StorageNotFoundError as e:
            raise AttachmentRefusedError(
                AttachmentErrorKind.FILE_NOT_FOUND,
                f"file not found: {key.in_workspace()}",
            ) from e
        except StorageError as e:
            raise AttachmentRefusedError(
                AttachmentErrorKind.STORAGE_ERROR,
                f"cannot read the file: {key.in_workspace()}: {e}",
            ) from e

    @classmethod
    def _resolve(cls, path: str, tool_call_id: str) -> AttachmentTarget:
        session = RequiredSession.of()
        user_id = session.user_id
        thread_id = session.thread_id

        turn = TurnContext.turn_of(thread_id)
        if turn is None:
            raise AttachmentRefusedError(
                AttachmentErrorKind.NO_TURN, "the turn is already finished"
            )

        for_id = turn.answer_step_id
        if not for_id:
            raise AttachmentRefusedError(
                AttachmentErrorKind.NO_TURN, "the turn has no answer step"
            )

        element_id = ChatView.derive_id(thread_id, tool_call_id, StepRole.ELEMENT)
        if not element_id:
            raise AttachmentRefusedError(
                AttachmentErrorKind.NO_TOOL_CALL, "tool call without id"
            )

        key = cls._key(user_id, thread_id, path)
        return AttachmentTarget(key=key, element_id=element_id, for_id=for_id)

    @staticmethod
    def _key(user_id: str, thread_id: str, path: str) -> ObjectKey:
        try:
            return ObjectKey.from_workspace(user_id, thread_id, path)
        except ValueError as e:
            raise AttachmentRefusedError(AttachmentErrorKind.BAD_PATH, str(e)) from e

    @classmethod
    async def _send(cls, target: AttachmentTarget) -> None:
        """Element.send сам пишет строку в elements и рассылает её вкладкам."""
        key = target.key
        mime = mimetypes.guess_type(key.name)[0]
        if not mime:
            mime = cls.FALLBACK_MIME

        element = WorkspaceFile(
            id=target.element_id,
            name=key.name,
            thread_id=key.thread_id,
            url=cls._layer().links.url(key.thread_id, target.element_id, key.dir),
            mime=mime,
            display="inline",
            props=ElementProps(dir=key.dir).model_dump(mode="json"),
        )
        await element.send(for_id=target.for_id)

    @staticmethod
    def _layer() -> AttachmentDataLayer:
        layer = get_data_layer()
        if not isinstance(layer, AttachmentDataLayer):
            msg = f"data layer does not address attachments: {type(layer)}"
            raise RuntimeError(msg)
        return layer


def build_send_file_tool() -> BaseTool:
    @tool(response_format="content_and_artifact")
    async def send_file(
        path: Annotated[
            str,
            Field(min_length=1, description=FileAttachment.PATH_DESCRIPTION),
        ],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> tuple[str, ToolResult]:
        """Отправить пользователю файл из workspace вложением в чат."""
        return pack_result(await FileAttachment.attach(path, tool_call_id))

    return send_file
