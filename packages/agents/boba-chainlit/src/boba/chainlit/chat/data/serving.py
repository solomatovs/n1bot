"""Отдача сохранённых вложений: элемент по id, mime из него, контент из storage."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, ClassVar
from uuid import UUID

from fastapi import Depends, HTTPException, Response

from boba.chainlit.chat.data.data_layer import PostgresDataLayer
from boba.chainlit.chat.data.object_key import ObjectKey
from boba.chainlit.chat.data.storage import LocalStorageClient
from chainlit.auth import get_current_user
from chainlit.user import PersistedUser, User

__all__ = ["AttachmentServing"]


class AttachmentServing:
    """GET-обработчик вложений: mime едет из elements, иначе фронт не рисует."""

    FALLBACK_MIME: ClassVar[str] = "application/octet-stream"

    def __init__(
        self,
        storage: LocalStorageClient,
        layer: Callable[[], PostgresDataLayer],
    ) -> None:
        self._storage = storage
        self._layer = layer

    async def serve(
        self,
        thread_id: UUID,
        element_id: UUID,
        current_user: Annotated[User | PersistedUser | None, Depends(get_current_user)],
    ) -> Response:
        if not isinstance(current_user, PersistedUser):
            raise HTTPException(status_code=401, detail="Unauthorized")

        element = await self._layer().get_element(str(thread_id), str(element_id))
        if element is None:
            raise HTTPException(status_code=404, detail="File not found")

        # путь от текущего пользователя: чужие образы недостижимы
        key = ObjectKey.build(
            current_user.id,
            element.get("threadId"),
            element.get("name"),
            element.get("id"),
        )
        try:
            content = await self._storage.read_file(key.render())
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail="File not found") from e

        mime = element.get("mime")
        if mime is None:
            mime = self.FALLBACK_MIME
        return Response(content=content, media_type=mime)
