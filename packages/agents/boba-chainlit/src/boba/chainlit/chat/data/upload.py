"""Загрузка и отдача вложения потоком: тело запроса не оседает ни в памяти, ни в tmp.

Штатный роут chainlit получает файл уже разобранным: FastAPI собирает multipart
целиком (до 1 МиБ в памяти, дальше во временный файл), затем `await file.read()`
поднимает его в память и `persist_file` пишет копию в каталог сессии. Здесь тело
разбирается по мере поступления и чанками уходит прямо в образ пользователя,
поэтому единственный предел размера — свободное место в образе.

Ошибки: HTTPException 400 — тело без файловой части или файл не прошёл проверку
chainlit; 401 — сессия чужая; 404 — сессия или родительское сообщение неизвестны;
507 — в образе не осталось места.
"""

from __future__ import annotations

import asyncio
import io
import logging
import mimetypes
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, ClassVar
from uuid import UUID
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, Response
from fastapi.datastructures import UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.datastructures import Headers

from boba.chainlit.chat.data.data_layer import PostgresDataLayer
from boba.chainlit.chat.data.fields import ElementField, FileField
from boba.chainlit.chat.data.object_key import ObjectKey
from boba.chainlit.chat.data.storage import LocalStorageClient, StorageFullError
from chainlit.auth import get_current_user
from chainlit.user import PersistedUser, User

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

__all__ = ["AttachmentServing", "MultipartFile", "UploadPolicy", "UploadRoute"]

logger = logging.getLogger(__name__)


class MultipartFile:
    """Файловая часть multipart-тела, читаемая по мере поступления запроса.

    Парсер python_multipart синхронный и работает на колбэках: `_pump` скармливает
    ему очередной сетевой чанк, а колбэки складывают распакованные данные в
    `_pending`, откуда их забирает `chunks`. За раз в памяти лежит столько, сколько
    принёс один чанк сети, независимо от размера файла.
    """

    DISPOSITION: ClassVar[bytes] = b"content-disposition"
    CONTENT_TYPE: ClassVar[bytes] = b"content-type"
    DEFAULT_MIME: ClassVar[str] = "application/octet-stream"

    def __init__(self, request: Request) -> None:
        self.filename: str = ""
        self.content_type: str = ""

        self._stream = request.stream().__aiter__()
        self._parser = MultipartParser(self._boundary(request), self._callbacks())
        self._headers: dict[bytes, bytes] = {}
        self._field = bytearray()
        self._value = bytearray()
        self._pending: list[bytes] = []
        self._found = False
        self._part_done = False
        self._eof = False

    async def open(self) -> None:
        """Дочитывает тело до конца заголовков файловой части."""
        while not self._found and not self._eof:
            await self._pump()

        if not self._found:
            raise HTTPException(status_code=400, detail="No file part in the request")

    async def chunks(self) -> AsyncIterator[bytes]:
        """Данные файловой части в порядке поступления."""
        while True:
            pending, self._pending = self._pending, []
            for chunk in pending:
                yield chunk

            if self._part_done or self._eof:
                return

            await self._pump()

    async def drain(self, max_bytes: int, max_seconds: float) -> int:
        """Выбрасывает то, что клиент успевает дослать; возвращает пропущенный объём.

        Аналог lingering_close в nginx. Закрыть сокет, пока клиент шлёт, нельзя:
        придёт RST, клиент потеряет уже отправленный ответ и повторит запрос.
        Поэтому входящее какое-то время вычитывается впустую — но с потолком, чтобы
        отвергнутый файл не заливался целиком.
        """
        deadline = time.monotonic() + max_seconds
        skipped = 0
        while not self._eof and skipped < max_bytes:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                await asyncio.wait_for(self._pump(), timeout=left)
            except TimeoutError:
                break
            for chunk in self._pending:
                skipped += len(chunk)
            self._pending.clear()
        return skipped

    async def _pump(self) -> None:
        try:
            chunk = await self._stream.__anext__()
        except StopAsyncIteration:
            self._eof = True
            self._parser.finalize()
            return

        self._parser.write(chunk)

    @classmethod
    def _boundary(cls, request: Request) -> bytes:
        content_type = request.headers.get("content-type", "")
        kind, options = parse_options_header(content_type)
        boundary = options.get(b"boundary")
        if kind != b"multipart/form-data" or not boundary:
            raise HTTPException(
                status_code=400, detail="Expected a multipart/form-data body"
            )
        return boundary

    def _callbacks(self) -> Any:
        return {
            "on_part_begin": self._on_part_begin,
            "on_header_field": self._on_header_field,
            "on_header_value": self._on_header_value,
            "on_header_end": self._on_header_end,
            "on_headers_finished": self._on_headers_finished,
            "on_part_data": self._on_part_data,
            "on_part_end": self._on_part_end,
        }

    def _on_part_begin(self) -> None:
        self._headers.clear()
        self._field.clear()
        self._value.clear()

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._field.extend(data[start:end])

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._value.extend(data[start:end])

    def _on_header_end(self) -> None:
        self._headers[bytes(self._field).lower()] = bytes(self._value)
        self._field.clear()
        self._value.clear()

    def _on_headers_finished(self) -> None:
        """Части без имени файла — обычные поля формы: они просто пропускаются."""
        _, options = parse_options_header(self._headers.get(self.DISPOSITION, b""))
        filename = options.get(b"filename")
        if not filename:
            return

        self.filename = filename.decode("utf-8", errors="replace")
        mime = self._headers.get(self.CONTENT_TYPE, b"").decode()
        self.content_type = mime or self.DEFAULT_MIME
        self._found = True

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        if not self._found or self._part_done:
            return
        self._pending.append(bytes(data[start:end]))

    def _on_part_end(self) -> None:
        if self._found:
            self._part_done = True


@dataclass(frozen=True, slots=True)
class UploadPolicy:
    """Маршруты и пределы потоковой загрузки вложений."""

    upload_path: str = "/project/file"
    download_path: str = "/project/file/{file_id}"
    no_space_status: int = 507
    object_key: str = "object_key"
    mib: int = 1024 * 1024
    log_every_bytes: int = 32 * 1024 * 1024
    drain_bytes: int = 8 * 1024 * 1024
    """Потолок вычитывания после отказа: дальше соединение закрывается."""
    drain_seconds: float = 3.0


class UploadRoute:
    """Замена chainlit-роутов вложения: файл течёт в хранилище и обратно."""

    CURRENT_USER: ClassVar[Any] = Depends(get_current_user)
    """Та же проверка токена, что и у chainlit; дефолт сигнатуры — только класс."""

    def __init__(self, storage: LocalStorageClient, policy: UploadPolicy) -> None:
        self._storage = storage
        self._policy = policy

    def install(self, chainlit_app: FastAPI) -> None:
        """Роуты встают перед chainlit-овскими: побеждает первый совпавший."""
        routes = [
            (self._policy.upload_path, self.upload, "POST"),
            (self._policy.download_path, self.download, "GET"),
        ]
        for path, endpoint, method in routes:
            chainlit_app.add_api_route(
                path, endpoint, methods=[method], include_in_schema=False
            )
            chainlit_app.router.routes.insert(0, chainlit_app.router.routes.pop())
            logger.info(
                "upload: %s %s handled by %s", method, path, type(self).__name__
            )

    async def upload(
        self,
        request: Request,
        session_id: str,
        ask_parent_id: str | None = None,
        current_user: Any = CURRENT_USER,
    ) -> JSONResponse:
        """Принимает файл и отдаёт ссылку в том же виде, что и chainlit."""
        started = time.monotonic()
        logger.info(
            "upload: request accepted, session=%s declared=%s bytes type=%s",
            session_id,
            request.headers.get("content-length", "unknown"),
            request.headers.get("content-type", "unknown"),
        )
        part = MultipartFile(request)
        try:
            session = self._session(session_id, current_user)

            await part.open()
            logger.info(
                "upload: headers parsed in %s, file=%r mime=%s",
                self._took(started),
                part.filename,
                part.content_type,
            )

            self._validate(session, part, request, ask_parent_id)

            key = ObjectKey.build(
                self._user_id(session), session.thread_id, part.filename, uuid.uuid4()
            )
            try:
                written = await self._store(key, part)
            except StorageFullError as e:
                logger.warning("upload: %s rejected, %s", key.render(), e)
                raise HTTPException(
                    status_code=self._policy.no_space_status, detail=str(e)
                ) from e
        except HTTPException:
            # клиент ещё шлёт файл: без этого соединение оборвётся и он повторит запрос
            skipped = await part.drain(self._policy.drain_bytes, self._policy.drain_seconds)
            logger.info(
                "upload: %s of the rejected body discarded", self._volume(skipped)
            )
            raise

        logger.info(
            "upload: %s done, %s in %s",
            key.render(),
            self._volume(written),
            self._took(started),
        )
        return JSONResponse(content=self._register(session, part, key, written))

    async def download(
        self,
        file_id: str,
        session_id: str,
        current_user: Any = CURRENT_USER,
    ) -> StreamingResponse:
        """Отдаёт ранее загруженный файл прямо из хранилища."""
        session = self._session(session_id, current_user)

        record = session.files.get(file_id)
        if not record or self._policy.object_key not in record:
            raise HTTPException(status_code=404, detail="File not found")

        name = quote(str(record[FileField.NAME]))
        logger.info(
            "upload: serving %s from storage (%s)",
            record[FileField.NAME],
            record[self._policy.object_key],
        )
        return StreamingResponse(
            self._storage.read_stream(str(record[self._policy.object_key])),
            media_type=str(record[FileField.TYPE]),
            headers={"Content-Disposition": f'inline; filename="{name}"'},
        )

    async def _store(self, key: ObjectKey, part: MultipartFile) -> int:
        """Переливает часть в хранилище и возвращает записанный объём."""
        written = 0
        started = time.monotonic()
        target = key.render()

        async def counted() -> AsyncIterator[bytes]:
            nonlocal written
            milestone = self._policy.log_every_bytes
            async for chunk in part.chunks():
                written += len(chunk)
                if written >= milestone:
                    milestone = written + self._policy.log_every_bytes
                    logger.info(
                        "upload: %s streaming, %s in %s (%s)",
                        target,
                        self._volume(written),
                        self._took(started),
                        self._rate(written, started),
                    )
                yield chunk

        logger.info("upload: %s streaming into storage", target)
        await self._storage.upload_stream(target, counted(), part.content_type)
        logger.info(
            "upload: %s written, %s in %s (%s)",
            target,
            self._volume(written),
            self._took(started),
            self._rate(written, started),
        )
        return written

    def _volume(self, written: int) -> str:
        return f"{written / self._policy.mib:.1f} MiB"

    @staticmethod
    def _took(started: float) -> str:
        return f"{time.monotonic() - started:.1f}s"

    def _rate(self, written: int, started: float) -> str:
        elapsed = time.monotonic() - started
        if elapsed <= 0:
            return "instant"
        return f"{written / self._policy.mib / elapsed:.1f} MiB/s"

    def _validate(
        self,
        session: Any,
        part: MultipartFile,
        request: Request,
        ask_parent_id: str | None,
    ) -> None:
        """Проверки chainlit по заголовкам: тело ещё не прочитано."""
        from chainlit.server import validate_file_upload  # noqa: PLC0415

        spec = session.files_spec.get(ask_parent_id, None)
        if not spec and ask_parent_id:
            raise HTTPException(status_code=404, detail="Parent message not found")

        # размер части заранее неизвестен: его верхняя оценка — длина всего тела
        declared = int(request.headers.get("content-length") or 0)
        probe = UploadFile(
            file=io.BytesIO(),
            size=declared,
            filename=part.filename,
            headers=Headers({"content-type": part.content_type}),
        )
        try:
            validate_file_upload(probe, spec=spec)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    def _register(
        self,
        session: Any,
        part: MultipartFile,
        key: ObjectKey,
        written: int,
    ) -> dict[str, Any]:
        """Запись о файле там же, где её ждут emitter и роут скачивания."""
        file_id = str(uuid.uuid4())
        suffix = mimetypes.guess_extension(part.content_type) or ""
        session.files[file_id] = {
            "id": file_id,
            # копии на диске нет: путь остаётся лишь именем для потребителей chainlit
            "path": session.files_dir / f"{file_id}{suffix}",
            "name": key.name,
            "type": part.content_type,
            "size": written,
            self._policy.object_key: key.render(),
        }
        return {
            "id": file_id,
            "name": key.name,
            "type": part.content_type,
            "size": written,
        }

    @staticmethod
    def _session(session_id: str, current_user: Any) -> Any:
        from chainlit.session import WebsocketSession  # noqa: PLC0415

        session = WebsocketSession.get_by_id(session_id) if session_id else None
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        if current_user and (
            not session.user or session.user.identifier != current_user.identifier
        ):
            raise HTTPException(
                status_code=401, detail="This session belongs to another user"
            )
        return session

    @staticmethod
    def _user_id(session: Any) -> str:
        user = session.user
        identifier = getattr(user, "id", None) or getattr(user, "identifier", None)
        if not identifier:
            raise HTTPException(status_code=401, detail="Session has no user")
        return str(identifier)


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
            element.get(ElementField.THREAD_ID),
            element.get(ElementField.NAME),
            element.get(ElementField.ID),
        )
        try:
            content = await self._storage.read_file(key.render())
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail="File not found") from e

        mime = element.get(ElementField.MIME)
        if mime is None:
            mime = self.FALLBACK_MIME
        return Response(content=content, media_type=mime)
