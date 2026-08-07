"""Tool send_file: отправляет файл из workspace вложением в чат.

Ошибки: ErrorResult — нет сессии, нет живого хода, путь вне каталога
вложений треда; остальное упаковывает ToolErrorGuard.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from typing import Annotated, ClassVar

from langchain.tools import tool
from langchain_core.tools import BaseTool, InjectedToolCallId
from pydantic import Field

import chainlit as cl
from boba.chainlit.chat.data.data_layer import AttachmentDataLayer
from boba.chainlit.chat.data.object_key import ObjectKey
from boba.chainlit.chat.turn import ChatTurn
from boba.chainlit.infra.session import current_thread_id, current_user_id
from boba.chainlit.rendering.chat_view import ChatView, StepRole
from boba.toolkit.result import ErrorResult, TextResult, ToolResult, pack_result
from chainlit.data import get_data_layer

__all__ = ["AttachmentRefusedError", "FileAttachment", "build_send_file_tool"]


class AttachmentRefusedError(Exception):
    """Файл отправить нельзя; текст причины готов для LLM."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class AttachmentTarget:
    """Разобранный запрос: файл, элемент и шаг, к которому он крепится."""

    key: ObjectKey
    element_id: str
    for_id: str


class FileAttachment:
    """Отправка файла из workspace штатным элементом chainlit."""

    PATH_DESCRIPTION: ClassVar[str] = (
        "Путь к файлу в каталоге вложений треда: "
        "'/workspace/<thread_id>/upload/<имя>'. Файл из другого места "
        "workspace сначала перенеси туда через bash."
    )

    FALLBACK_MIME: ClassVar[str] = "application/octet-stream"

    @classmethod
    async def attach(cls, path: str, tool_call_id: str) -> ToolResult:
        try:
            target = cls._resolve(path, tool_call_id)
        except AttachmentRefusedError as e:
            return ErrorResult(message=str(e), error_kind=e.kind)

        await cls._send(target)
        return TextResult(
            text=f"file attached to the chat: {target.key.name}",
            metadata={"path": target.key.in_workspace()},
        )

    @classmethod
    def _resolve(cls, path: str, tool_call_id: str) -> AttachmentTarget:
        user_id = current_user_id()
        if not user_id:
            raise AttachmentRefusedError("no_session", "no chainlit user session")

        thread_id = current_thread_id()
        if not thread_id:
            raise AttachmentRefusedError("no_thread", "no active thread")

        turn = ChatTurn.active(thread_id)
        if turn is None:
            raise AttachmentRefusedError("no_turn", "the turn is already finished")

        for_id = turn.answer_step_id
        if not for_id:
            raise AttachmentRefusedError("no_turn", "the turn has no answer step")

        element_id = ChatView.derive_id(thread_id, tool_call_id, StepRole.ELEMENT)
        if not element_id:
            raise AttachmentRefusedError("no_tool_call", "tool call without id")

        key = cls._key(user_id, thread_id, path)
        return AttachmentTarget(key=key, element_id=element_id, for_id=for_id)

    @staticmethod
    def _key(user_id: str, thread_id: str, path: str) -> ObjectKey:
        try:
            return ObjectKey.from_workspace(user_id, thread_id, path)
        except ValueError as e:
            raise AttachmentRefusedError("bad_path", str(e)) from e

    @classmethod
    async def _send(cls, target: AttachmentTarget) -> None:
        """Element.send сам пишет строку в elements и рассылает её вкладкам."""
        key = target.key
        mime = mimetypes.guess_type(key.name)[0]
        if not mime:
            mime = cls.FALLBACK_MIME

        element = cl.File(
            id=target.element_id,
            name=key.name,
            thread_id=key.thread_id,
            url=cls._layer().links.url(key.thread_id, target.element_id),
            mime=mime,
            display="inline",
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
