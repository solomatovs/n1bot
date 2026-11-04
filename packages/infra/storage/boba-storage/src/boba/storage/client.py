"""Общая потоковая граница хранилища и её реализация на локальном диске.

Ошибки:
StorageError — чтение, запись или удаление не выполнено.
StorageNotFoundError — объект отсутствует.
StorageFullError — недостаточно места для записи."""

from __future__ import annotations

import stat as stat_module
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from typing import Any, Literal

from anyio import Path, open_file
from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field

from boba.canvas.keys import DirKey, ScopeDir, WorkspaceMount
from boba.canvas.storage import (
    FileStat,
    OpenedStream,
    ReadWindow,
    StorageError,
    StorageGuard,
    StorageOp,
    StorageUrl,
)
from boba.config.paths import PathsConfig
from boba.core.components import Components
from boba.toolkit.result import ContentMime


class StorageConfig(BaseModel, ABC):
    """Общие настройки публикации файлов; остальные поля задаёт реализация."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    public_prefix: str = "/upload"

    @abstractmethod
    def read_chunk_bytes(self) -> int: ...


class LocalStorageConfig(StorageConfig):
    """Настройки файлового хранилища для LocalStorageClient: каталог и размер
    блока чтения. StorageFactory выбирает его по kind=local."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["local"] = "local"
    files_dir: str = Field(min_length=1)
    public_prefix: str = Field(default="/upload")
    chunk_bytes: int = Field(
        default=65536,
        gt=0,
        validation_alias=AliasChoices(
            "chunk_bytes", AliasPath("mounting", "copy_chunk_bytes")
        ),
        description="Размер блока потокового чтения файлов, байт.",
    )

    def read_chunk_bytes(self) -> int:
        return self.chunk_bytes


class StorageClient(ABC):
    """Фасад хранилища: потоковые операции и их граница ошибок.

    Запись и чтение идут только чанками: метода, который принял бы или
    вернул содержимое целиком, у хранилища нет.

    Каждая операция идёт через StorageGuard, поэтому наружу выходят только
    StorageError и его подклассы:

    * upload_stream — StorageFullError, когда места нет; StorageError на
      любом другом отказе записи;
    * stat, open_stream — StorageNotFoundError, когда объекта нет;
      StorageError на отказе чтения;
    * delete_file — False, когда объекта нет; StorageError на отказе;
    * list_dir — пустая последовательность, когда каталога нет;
      StorageError на отказе;
    * delete_scope — область без файлов не ошибка; StorageError на отказе.

    Реализации пишут операции в защищённых методах и публичные не
    переопределяют — иначе ошибка уйдёт мимо границы.
    """

    def __init__(self, public_prefix: str, chunk_bytes: int) -> None:
        self._public_prefix = public_prefix
        self._chunk_bytes = chunk_bytes

    def render_url(self, url: str, object_key: str) -> str:
        return StorageUrl.render(url, self._public_prefix, object_key)

    async def get_read_url(self, object_key: str) -> str:
        return self.render_url(StorageUrl.TEMPLATE, object_key)

    async def close(self) -> None:
        """Хранилище без долгоживущих ресурсов: закрывать нечего."""
        return

    async def workspace(self, user_id: str, scope_id: str) -> str:
        """Каталог workspace, под которым песочница видит файлы области: у
        хранилища процесса — точка workspace его песочницы; хранилище на
        сервере файлов отвечает каталогом, который объявил сервер.

        Ошибки:
        StorageError — каталог узнать негде.
        """
        return WorkspaceMount.path()

    @staticmethod
    def _uploaded(object_key: str) -> dict[str, Any]:
        return {"object_key": object_key, "url": StorageUrl.TEMPLATE.value}

    async def upload_stream(
        self,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str = ContentMime.OCTET.value,
    ) -> dict[str, Any]:
        with StorageGuard(StorageOp.WRITE, object_key):
            return await self._upload_stream(object_key, source, mime)

    async def stat(self, object_key: str) -> FileStat:
        with StorageGuard(StorageOp.STAT, object_key):
            return await self._stat(object_key)

    async def open_stream(self, object_key: str, window: ReadWindow) -> OpenedStream:
        """Открывает объект на чтение: существование и размер — до тела."""
        with StorageGuard(StorageOp.READ, object_key):
            opened = await self._open_stream(object_key, window)

        body = self._guarded_chunks(object_key, opened.chunks)
        return OpenedStream(stat=opened.stat, chunks=body, release=opened.release)

    @staticmethod
    async def _no_release() -> None:
        """Поток без своего процесса: освобождать нечего."""
        return

    async def disk_source(self, path: str) -> AsyncGenerator[bytes, None]:
        """Файл локального диска как источник чанков для upload_stream."""
        chunk_bytes = self._chunk_bytes

        async with await open_file(path, "rb") as f:
            while True:
                chunk = await f.read(chunk_bytes)
                if not chunk:
                    break

                yield chunk

    async def delete_file(self, object_key: str) -> bool:
        with StorageGuard(StorageOp.DELETE, object_key):
            return await self._delete_file(object_key)

    async def list_dir(self, prefix: str) -> Sequence[str]:
        with StorageGuard(StorageOp.LIST, prefix):
            return await self._list_dir(prefix)

    async def delete_scope(self, user_id: str, scope_id: str) -> None:
        """Все файлы области пользователя во всех её каталогах."""
        with StorageGuard(StorageOp.DELETE, f"scope {scope_id} of user {user_id}"):
            await self._delete_scope(user_id, scope_id)

    async def _guarded_chunks(
        self, object_key: str, chunks: AsyncGenerator[bytes, None]
    ) -> AsyncGenerator[bytes, None]:
        """Тело итерируется после возврата open_stream: границе нужен свой guard."""
        with StorageGuard(StorageOp.READ, object_key):
            async for chunk in chunks:
                yield chunk

    @abstractmethod
    async def _upload_stream(
        self,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def _stat(self, object_key: str) -> FileStat: ...

    @abstractmethod
    async def _open_stream(
        self, object_key: str, window: ReadWindow
    ) -> OpenedStream: ...

    @abstractmethod
    async def _delete_file(self, object_key: str) -> bool: ...

    @abstractmethod
    async def _list_dir(self, prefix: str) -> Sequence[str]: ...

    async def _delete_scope(self, user_id: str, scope_id: str) -> None:
        """Каталог за каталогом: листинг и удаление каждого файла; хранилище,
        которое удаляет область одной операцией, переопределяет."""
        for scope_dir in ScopeDir:
            dir_key = DirKey.of(user_id, scope_id, scope_dir)
            for name in await self._list_dir(dir_key.render()):
                await self._delete_file(dir_key.file(name).render())


class StorageFactory:
    """Собирает хранилище зарегистрированной фабрикой; конфиг уже проверен входом."""

    def __init__(
        self,
        instance: PathsConfig,
        backends: Components[StorageConfig, PathsConfig, StorageClient],
    ) -> None:
        self._instance = instance
        self._backends = backends

    def create(self, config: StorageConfig) -> StorageClient:
        try:
            return self._backends.build(config, self._instance)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(
                f"creating storage {config.kind!r} failed: {exc}"
            ) from exc


class LocalStorageClient(StorageClient):
    """Хранит файлы вложений на локальном диске под files_dir."""

    def __init__(self, config: LocalStorageConfig) -> None:
        super().__init__(config.public_prefix, config.chunk_bytes)
        self._config = config

    async def _resolve(self, object_key: str) -> Path:
        base_dir = await Path(self._config.files_dir).resolve()
        path = await (base_dir / object_key).resolve()

        if not path.is_relative_to(base_dir):
            msg = (
                f"storage: object_key {object_key!r} resolves to {path}, "
                f"outside files_dir {base_dir}"
            )
            raise StorageError(msg)

        return path

    async def _upload_stream(
        self,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str,
    ) -> dict[str, Any]:
        """Кладёт файл чанками: содержимое целиком в память не поднимается."""
        path = await self._resolve(object_key)
        await path.parent.mkdir(parents=True, exist_ok=True)

        async with await path.open("wb") as f:
            async for chunk in source:
                await f.write(chunk)

        return self._uploaded(object_key)

    async def _stat(self, object_key: str) -> FileStat:
        path = await self._resolve(object_key)
        return await self._stat_path(path)

    async def _open_stream(self, object_key: str, window: ReadWindow) -> OpenedStream:
        path = await self._resolve(object_key)

        stat = await self._stat_path(path)
        body = self._window_chunks(path, window, stat.size)
        return OpenedStream(stat=stat, chunks=body, release=self._no_release)

    @staticmethod
    async def _stat_path(path: Path) -> FileStat:
        result = await path.stat()

        if not stat_module.S_ISREG(result.st_mode):
            msg = f"storage: {path} is not a regular file, mode {result.st_mode:o}"
            raise StorageError(msg)

        return FileStat(size=result.st_size, revision=result.st_mtime_ns)

    async def _window_chunks(
        self, path: Path, window: ReadWindow, size: int
    ) -> AsyncGenerator[bytes, None]:
        """Отдаёт окно файла чанками: в памяти живёт один чанк, а не файл."""
        chunk_bytes = self._config.chunk_bytes
        remaining = window.resolve_length(size)

        async with await path.open("rb") as f:
            await f.seek(window.offset)

            while remaining > 0:
                chunk = await f.read(min(chunk_bytes, remaining))
                if not chunk:
                    break

                remaining -= len(chunk)
                yield chunk

    async def _delete_file(self, object_key: str) -> bool:
        path = await self._resolve(object_key)
        try:
            await path.unlink()
        except FileNotFoundError:
            return False

        return True

    async def _list_dir(self, prefix: str) -> Sequence[str]:
        path = await self._resolve(prefix)
        names: list[str] = []
        try:
            async for entry in path.iterdir():
                if not await entry.is_file():
                    continue

                names.append(entry.name)
        except (FileNotFoundError, NotADirectoryError):
            return ()

        return tuple(sorted(names))
