"""Tool send_file: отправляет файл из workspace вложением в чат.

Ошибки: ErrorResult — нет контекста вызова, нет живого хода, путь вне каталога
вложений треда, файла нет или хранилище его не отдало; остальное
упаковывает ToolErrorGuard.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, ClassVar

from langchain_core.tools import BaseTool, tool
from pydantic import Field

import chainlit as cl
from boba.canvas.keys import ElementProps, ObjectKey
from boba.canvas.storage import StorageError, StorageNotFoundError
from boba.chainlit.data.data_layer import AttachmentDataLayer
from boba.chainlit.domain.context import ChatCallContext
from boba.identity.errors import RefusalError
from boba.identity.run import ElementTarget, RunRegistry
from boba.toolkit.result import ErrorResult, TextResult, ToolResult, pack_result

__all__ = [
    "AttachmentErrorKind",
    "AttachmentRefusedError",
    "FileAttachment",
    "WorkspaceFile",
    "build_send_file_tool",
]


class AttachmentErrorKind(StrEnum):
    """Коды отказов send_file: уезжают в ErrorResult.error_kind."""

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
    target: ElementTarget


class FileAttachment:
    """Отправка файла из workspace штатным элементом chainlit."""

    PATH_DESCRIPTION: ClassVar[str] = (
        "Путь к файлу в каталогах треда: '/workspace/<thread_id>/upload/<имя>' "
        "или '/workspace/<thread_id>/mermaid/<имя>'. Файл из другого места "
        "workspace сначала перенеси туда через bash."
    )

    FALLBACK_MIME: ClassVar[str] = "application/octet-stream"

    @classmethod
    async def attach(cls, path: str) -> ToolResult:
        try:
            target = cls._resolve(path)
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
            await AttachmentDataLayer.require().storage.stat(key.render())
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
    def _resolve(cls, path: str) -> AttachmentTarget:
        context = ChatCallContext.require()
        user_id = context.subject.user_key
        thread_id = context.scope.id

        port = RunRegistry.require_port(thread_id)
        target = port.element_target(context.tool_call_id())
        key = cls._key(user_id, thread_id, path)

        return AttachmentTarget(key=key, target=target)

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

        element_id = target.target.element_id
        links = AttachmentDataLayer.require().links
        element = WorkspaceFile(
            id=element_id,
            name=key.name,
            thread_id=key.thread_id,
            url=links.url(key.thread_id, element_id, key.dir),
            mime=mime,
            display="inline",
            props=ElementProps(dir=key.dir).model_dump(mode="json"),
        )
        await element.send(for_id=target.target.for_id)


def build_send_file_tool() -> BaseTool:
    @tool(response_format="content_and_artifact")
    async def send_file(
        path: Annotated[
            str,
            Field(min_length=1, description=FileAttachment.PATH_DESCRIPTION),
        ],
    ) -> tuple[str, ToolResult]:
        """Отправить пользователю файл из workspace вложением в чат."""
        return pack_result(await FileAttachment.attach(path))

    return send_file
