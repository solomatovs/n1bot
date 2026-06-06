"""Порт `UserCatalog` + FS-реализация поверх `users.json` в WorkspaceShell."""

from __future__ import annotations

import asyncio
import io
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import TypeAdapter

from boba.chainlit.models import StoredUser, UserId
from boba.workspace.contract import WorkspaceShell

__all__ = ["FsUserCatalog", "UserCatalog"]


_USER_CATALOG_ADAPTER: TypeAdapter[dict[str, StoredUser]] = TypeAdapter(
    dict[str, StoredUser],
)


class UserCatalog(ABC):
    """Источник users (auth-каталог)."""

    @abstractmethod
    async def get(self, identifier: str) -> StoredUser | None: ...

    @abstractmethod
    async def upsert(
        self, identifier: str, display_name: str | None, metadata: dict[str, Any]
    ) -> StoredUser: ...


class FsUserCatalog(UserCatalog):
    """JSON-файл `{identifier -> StoredUser}` внутри `WorkspaceShell`."""

    _DEFAULT_FILENAME: ClassVar[str] = "users.json"

    def __init__(
        self,
        shell: WorkspaceShell,
        filename: str = _DEFAULT_FILENAME,
    ) -> None:
        self._shell = shell
        self._filename = filename
        self._lock = asyncio.Lock()

    async def get(self, identifier: str) -> StoredUser | None:
        async with self._lock:
            data = await asyncio.to_thread(self._load)
            return data.get(identifier)

    async def upsert(
        self, identifier: str, display_name: str | None, metadata: dict[str, Any]
    ) -> StoredUser:
        async with self._lock:
            data = await asyncio.to_thread(self._load)
            user = data.get(identifier) or StoredUser(
                id=UserId(str(uuid.uuid4())),
                identifier=identifier,
                display_name=display_name,
                metadata=dict(metadata),
                created_at=datetime.now(UTC).isoformat(),
            )
            data[identifier] = user
            await asyncio.to_thread(self._save, data)
            return user

    def _load(self) -> dict[str, StoredUser]:
        if not self._shell.exists(self._filename):
            return {}
        with self._shell.read_text(self._filename) as fh:
            text = fh.read() or "{}"
        return _USER_CATALOG_ADAPTER.validate_json(text)

    def _save(self, data: dict[str, StoredUser]) -> None:
        payload = _USER_CATALOG_ADAPTER.dump_json(
            data,
            by_alias=True,
            indent=2,
        ).decode("utf-8")
        self._shell.atomic_write_text(self._filename, io.StringIO(payload))
