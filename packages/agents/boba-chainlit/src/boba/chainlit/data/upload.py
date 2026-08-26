"""Загрузка и отдача вложения потоком: тело не оседает ни в памяти, ни в tmp.

Штатный роут chainlit получает файл уже разобранным: FastAPI собирает multipart
целиком (до 1 МиБ в памяти, дальше во временный файл), затем `await file.read()`
поднимает его в память и `persist_file` пишет копию в каталог сессии. Здесь тело
разбирается по мере поступления и чанками уходит прямо в образ пользователя,
поэтому единственный предел размера — свободное место в образе. Отдача тоже
потоковая: тело идёт окном чанков из хранилища, заголовок Range отвечает 206.

Ошибки: HTTPException 400 — тело без файловой части или файл не прошёл проверку
chainlit; 401 — сессия чужая; 404 — сессия, родительское сообщение или файл
неизвестны; 416 — диапазон за концом файла; 507 — в образе не осталось места.
"""

from __future__ import annotations

import asyncio
import io
import logging
import mimetypes
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.datastructures import UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.datastructures import Headers

from boba.canvas.journal import StreamJournalError, StreamJournalHub, StreamKey
from boba.canvas.keys import ObjectKey, ThreadDir
from boba.canvas.storage import OpenedStream, StorageFullError, StorageNotFoundError
from boba.canvas.transfer import (
    ContentDisposition,
    FileHeader,
    TransferFormat,
    TransferProgress,
    UploadPolicy,
)
from boba.chainlit.data.storage import LocalStorageClient, StorageClient
from boba.chainlit.domain.config import LocalStorageConfig
from boba.chainlit.domain.fields import ElementField, FileField
from boba.chainlit.infra.session import ChainlitSession, session_source_ref
from boba.toolkit.channels import JournalChannels, ToolChannel
from boba.workspace.launcher import ReadWindow
from chainlit.auth import get_current_user
from chainlit.data.base import BaseDataLayer
from chainlit.user import PersistedUser, User

__all__ = [
    "AttachmentServing",
    "ByteRange",
    "CanvasServing",
    "MultipartFile",
    "RangeHeader",
    "SessionFiles",
    "StreamServing",
    "StreamedFile",
    "SuffixRange",
    "UploadRoute",
]

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
class ByteRange:
    """Диапазон bytes=start-end: end включительно, None — до конца файла."""

    start: int
    end: int | None

    def window(self) -> ReadWindow:
        if self.end is None:
            return ReadWindow(offset=self.start, length=None)

        return ReadWindow(offset=self.start, length=self.end - self.start + 1)


@dataclass(frozen=True, slots=True)
class SuffixRange:
    """Диапазон bytes=-N: последние N байт, окно зависит от размера файла."""

    length: int

    def window(self, size: int) -> ReadWindow:
        offset = max(size - self.length, 0)
        return ReadWindow(offset=offset, length=None)


class RangeHeader:
    """Разбор заголовка Range: только одиночный диапазон байтов."""

    UNIT: ClassVar[str] = "bytes="

    @classmethod
    def parse(cls, header: str) -> ByteRange | SuffixRange | None:
        """None — заголовок не разобран: RFC 9110 позволяет его игнорировать."""
        value = header.strip()
        if not value.startswith(cls.UNIT):
            return None

        spec = value[len(cls.UNIT) :].strip()
        if "," in spec:
            return None

        start_text, sep, end_text = spec.partition("-")
        if not sep:
            return None

        start_text = start_text.strip()
        end_text = end_text.strip()

        if not start_text:
            return cls._suffix(end_text)

        return cls._bounded(start_text, end_text)

    @staticmethod
    def _suffix(end_text: str) -> SuffixRange | None:
        if not end_text.isdigit():
            return None

        length = int(end_text)
        if length == 0:
            return None

        return SuffixRange(length=length)

    @staticmethod
    def _bounded(start_text: str, end_text: str) -> ByteRange | None:
        if not start_text.isdigit():
            return None

        start = int(start_text)
        if not end_text:
            return ByteRange(start=start, end=None)

        if not end_text.isdigit():
            return None

        end = int(end_text)
        if end < start:
            return None

        return ByteRange(start=start, end=end)


class StreamedFile:
    """HTTP-ответ телом объекта хранилища: стрим чанков, Content-Length, Range.

    Ход отдачи пишется в лог: строка перед первым байтом, отметки по мере
    передачи и итог. Обрыв клиента — обычное дело (перемотка видео, закрытая
    вкладка), поэтому недоотданное тело отмечается отдельно.
    """

    ACCEPT_RANGES: ClassVar[str] = "bytes"

    def __init__(self, storage: StorageClient, policy: UploadPolicy) -> None:
        self._storage = storage
        self._policy = policy
        self._format = TransferFormat(policy.mib)

    async def respond(
        self,
        object_key: str,
        *,
        mime: str,
        range_header: str,
        content_disposition: str,
    ) -> Response:
        """Ответ 200/206/416; нет объекта — HTTPException 404 до первого байта."""
        try:
            return await self._respond(
                object_key, mime, range_header, content_disposition
            )
        except StorageNotFoundError as e:
            raise HTTPException(status_code=404, detail="File not found") from e

    async def _respond(
        self,
        object_key: str,
        mime: str,
        range_header: str,
        content_disposition: str,
    ) -> Response:
        parsed = None
        if range_header:
            parsed = RangeHeader.parse(range_header)

        if isinstance(parsed, SuffixRange):
            stat = await self._storage.stat(object_key)
            window = parsed.window(stat.size)
            return await self._ranged(object_key, window, mime, content_disposition)

        if isinstance(parsed, ByteRange):
            window = parsed.window()
            return await self._ranged(object_key, window, mime, content_disposition)

        opened = await self._storage.open_stream(object_key, ReadWindow.entire())
        return self._full(object_key, opened, mime, content_disposition)

    async def _ranged(
        self,
        object_key: str,
        window: ReadWindow,
        mime: str,
        content_disposition: str,
    ) -> Response:
        opened = await self._storage.open_stream(object_key, window)

        if window.offset >= opened.stat.size:
            await opened.close()
            headers: dict[str, str] = {
                FileHeader.CONTENT_RANGE: f"bytes */{opened.stat.size}",
            }
            raise HTTPException(
                status_code=416, detail="Range out of bounds", headers=headers
            )

        return self._partial(object_key, opened, window, mime, content_disposition)

    def _full(
        self,
        object_key: str,
        opened: OpenedStream,
        mime: str,
        content_disposition: str,
    ) -> Response:
        headers: dict[str, str] = {
            FileHeader.CONTENT_LENGTH: str(opened.stat.size),
            FileHeader.ACCEPT_RANGES: self.ACCEPT_RANGES,
        }
        if content_disposition:
            headers[FileHeader.CONTENT_DISPOSITION] = content_disposition

        body = self._logged(object_key, opened, opened.stat.size, "whole file")
        return StreamingResponse(body, media_type=mime, headers=headers)

    def _partial(
        self,
        object_key: str,
        opened: OpenedStream,
        window: ReadWindow,
        mime: str,
        content_disposition: str,
    ) -> Response:
        size = opened.stat.size
        length = window.resolve_length(size)
        last = window.offset + length - 1

        headers: dict[str, str] = {
            FileHeader.CONTENT_LENGTH: str(length),
            FileHeader.CONTENT_RANGE: f"bytes {window.offset}-{last}/{size}",
            FileHeader.ACCEPT_RANGES: self.ACCEPT_RANGES,
        }
        if content_disposition:
            headers[FileHeader.CONTENT_DISPOSITION] = content_disposition

        window_label = f"bytes {window.offset}-{last} of {size}"
        body = self._logged(object_key, opened, length, window_label)
        return StreamingResponse(
            body, status_code=206, media_type=mime, headers=headers
        )

    async def _logged(
        self,
        object_key: str,
        opened: OpenedStream,
        total: int,
        window_label: str,
    ) -> AsyncIterator[bytes]:
        """Отдаёт тело, отмечая в логе начало, ход и итог передачи."""
        progress = TransferProgress(self._format, self._policy.serve_log_every_bytes)
        logger.info(
            "serving: %s sending %s (%s)",
            object_key,
            self._format.volume(total),
            window_label,
        )

        complete = False
        try:
            async for chunk in opened.chunks:
                if progress.advance(len(chunk)):
                    logger.info(
                        "serving: %s streaming, %s of %s (%s, %s)",
                        object_key,
                        progress.volume(),
                        self._format.volume(total),
                        progress.share(total),
                        progress.rate(),
                    )

                yield chunk

            complete = True
        finally:
            self._finished(object_key, progress, total, complete=complete)

    def _finished(
        self,
        object_key: str,
        progress: TransferProgress,
        total: int,
        *,
        complete: bool,
    ) -> None:
        if complete:
            logger.info(
                "serving: %s sent, %s in %s (%s)",
                object_key,
                progress.volume(),
                progress.took(),
                progress.rate(),
            )
            return

        logger.warning(
            "serving: %s aborted by the client, %s of %s sent (%s) in %s",
            object_key,
            progress.volume(),
            self._format.volume(total),
            progress.share(total),
            progress.took(),
        )


class SessionFiles:
    """Файлы сессии, которые роут отдаёт прямо из хранилища.

    Ключ object_key — расширение чужого FileDict: по нему download узнаёт, что
    тело файла лежит в storage, а не на диске chainlit.
    """

    OBJECT_KEY: ClassVar[str] = "object_key"
    """Поле записи: тело файла лежит в storage по этому ключу."""

    ROOT_PATH_ENV: ClassVar[str] = "CHAINLIT_ROOT_PATH"
    """Префикс подмонтированного приложения; ссылка без него уйдёт в корень домена."""

    @classmethod
    def register(
        cls, session: ChainlitSession, key: ObjectKey, *, mime: str, size: int
    ) -> str:
        """Регистрирует объект хранилища как файл сессии; отдаёт его id."""
        file_id = str(uuid.uuid4())
        suffix = mimetypes.guess_extension(mime) or ""
        session.files[file_id] = {
            "id": file_id,
            # копии на диске нет: путь остаётся лишь именем для потребителей chainlit
            "path": session.files_dir / f"{file_id}{suffix}",
            "name": key.name,
            "type": mime,
            "size": size,
            cls.OBJECT_KEY: key.render(),
        }
        return file_id

    @classmethod
    def url_for(cls, session: ChainlitSession, file_id: str) -> str:
        """Ссылка на файл сессии — тот же вид, что строит фронт по chainlit_key."""
        prefix = os.getenv(cls.ROOT_PATH_ENV, "").rstrip("/")
        return f"{prefix}/project/file/{file_id}?session_id={session.id}"


class UploadRoute:
    """Замена chainlit-роутов вложения: файл течёт в хранилище и обратно."""

    CURRENT_USER: ClassVar[Any] = Depends(get_current_user)
    """Та же проверка токена, что и у chainlit; дефолт сигнатуры — только класс."""

    def __init__(self, storage: StorageClient, policy: UploadPolicy) -> None:
        self._storage = storage
        self._policy = policy
        self._format = TransferFormat(policy.mib)
        self._files = StreamedFile(storage, policy)

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
            skipped = await part.drain(
                self._policy.drain_bytes,
                self._policy.drain_seconds,
            )
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
        request: Request,
        file_id: str,
        session_id: str,
        current_user: Any = CURRENT_USER,
    ) -> Response:
        """Отдаёт ранее загруженный файл потоком прямо из хранилища."""
        session = self._session(session_id, current_user)

        record = session.files.get(file_id)
        if not record or SessionFiles.OBJECT_KEY not in record:
            raise HTTPException(status_code=404, detail="File not found")

        name = str(record[FileField.NAME])
        logger.info(
            "upload: serving %s from storage (%s)",
            name,
            record[SessionFiles.OBJECT_KEY],
        )
        return await self._files.respond(
            str(record[SessionFiles.OBJECT_KEY]),
            mime=str(record[FileField.TYPE]),
            range_header=request.headers.get(FileHeader.RANGE, ""),
            content_disposition=ContentDisposition.inline(name),
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
        return self._format.volume(written)

    def _took(self, started: float) -> str:
        return self._format.took(started)

    def _rate(self, written: int, started: float) -> str:
        return self._format.rate(written, started)

    def _validate(
        self,
        session: ChainlitSession,
        part: MultipartFile,
        request: Request,
        ask_parent_id: str | None,
    ) -> None:
        """Проверки chainlit по заголовкам: тело ещё не прочитано."""
        from chainlit.server import validate_file_upload  # noqa: PLC0415

        spec = session.file_spec(ask_parent_id)
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
        file_id = SessionFiles.register(
            session, key, mime=part.content_type, size=written
        )
        return {
            "id": file_id,
            "name": key.name,
            "type": part.content_type,
            "size": written,
        }

    @staticmethod
    def _session(session_id: str, current_user: Any) -> ChainlitSession:
        session = session_source_ref().by_id(session_id)
        if not session.present:
            raise HTTPException(status_code=404, detail="Session not found")

        if not current_user:
            return session

        if session.identifier != current_user.identifier:
            raise HTTPException(
                status_code=401, detail="This session belongs to another user"
            )

        return session

    @staticmethod
    def _user_id(session: ChainlitSession) -> str:
        """Ключ вложений — id строки users; логин в него не подставляется.

        Логин у пользователя меняется (регистр, переименование в каталоге),
        и подстановка увела бы часть вложений в чужой префикс.
        """
        user_id = session.user_id
        if not user_id:
            raise HTTPException(status_code=401, detail="Session has no persisted user")

        return str(user_id)


class AttachmentServing:
    """GET-обработчик вложений: mime едет из elements, иначе фронт не рисует."""

    FALLBACK_MIME: ClassVar[str] = "application/octet-stream"

    def __init__(
        self,
        storage: StorageClient,
        layer: Callable[[], BaseDataLayer],
        policy: UploadPolicy,
    ) -> None:
        self._storage = storage
        self._layer = layer
        self._files = StreamedFile(storage, policy)

    async def serve(
        self,
        request: Request,
        thread_id: UUID,
        dir: ThreadDir,  # noqa: A002
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
            dir_thread=dir,
        )

        mime = element.get(ElementField.MIME)
        if mime is None:
            mime = self.FALLBACK_MIME

        return await self._files.respond(
            key.render(),
            mime=mime,
            range_header=request.headers.get(FileHeader.RANGE, ""),
            content_disposition="",
        )


class CanvasServing:
    """Отдаёт файл панели по треду, каталогу и имени; сессия здесь ни при чём.

    Содержимое панели переживает и переподключение вкладки, и перезапуск
    приложения, поэтому ссылка адресует файл его местом в workspace, а
    пользователя берёт из токена — как это делает отдача журналов.
    """

    FALLBACK_MIME: ClassVar[str] = "application/octet-stream"

    def __init__(self, storage: StorageClient, policy: UploadPolicy) -> None:
        self._files = StreamedFile(storage, policy)

    async def serve(
        self,
        request: Request,
        thread_id: str,
        dir: ThreadDir,  # noqa: A002
        name: str,
        current_user: Annotated[User | PersistedUser | None, Depends(get_current_user)],
    ) -> Response:
        if not isinstance(current_user, PersistedUser):
            raise HTTPException(status_code=401, detail="Unauthorized")

        try:
            key = ObjectKey(
                user_id=str(current_user.id),
                thread_id=thread_id,
                name=name,
                dir=dir,
            )
        except ValidationError as e:
            raise HTTPException(status_code=404, detail="File not found") from e

        mime = mimetypes.guess_type(key.name)[0]
        if not mime:
            mime = self.FALLBACK_MIME

        return await self._files.respond(
            key.render(),
            mime=mime,
            range_header=request.headers.get(FileHeader.RANGE, ""),
            content_disposition="",
        )


class StreamServing:
    """Отдаёт .log вызова из тома журнала тем же StreamedFile, что и вложения.

    Журнал лежит в каталоге служебного тома — обычная ФС, поэтому источником
    служит LocalStorageClient над корнем тома пользователя; клиент на том
    кэшируется.
    """

    MIME: ClassVar[str] = "text/plain; charset=utf-8"

    def __init__(self, config: LocalStorageConfig, policy: UploadPolicy) -> None:
        self._config = config
        self._policy = policy
        self._files: dict[str, StreamedFile] = {}

    async def serve(
        self,
        request: Request,
        thread_id: str,
        call_id: str,
        current_user: Annotated[User | PersistedUser | None, Depends(get_current_user)],
        channel: str = ToolChannel.STDOUT.value,
    ) -> Response:
        if not isinstance(current_user, PersistedUser):
            raise HTTPException(status_code=401, detail="Unauthorized")

        journal = StreamJournalHub.get()
        if journal is None:
            raise HTTPException(status_code=404, detail="Stream not found")

        # служебные каналы вызова наружу не отдаются: скачать можно то же,
        # что показывает панель
        log_channel = JournalChannels.parse_visible(channel)
        if log_channel is None:
            raise HTTPException(status_code=404, detail="Stream not found")

        try:
            key = StreamKey(
                user_id=str(current_user.id), thread_id=thread_id, call_id=call_id
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail="Stream not found") from e

        try:
            root = journal.vault_root(key.user_id)
        except StreamJournalError as e:
            raise HTTPException(
                status_code=503, detail="Stream vault unavailable"
            ) from e

        rel_log = journal.log_rel_path(key, log_channel)
        if rel_log is None:
            raise HTTPException(status_code=404, detail="Stream not found")

        disposition = f'attachment; filename="{key.call_id}.{log_channel}.log"'
        return await self._files_for(root).respond(
            rel_log,
            mime=self.MIME,
            range_header=request.headers.get(FileHeader.RANGE, ""),
            content_disposition=disposition,
        )

    def _files_for(self, root: str) -> StreamedFile:
        files = self._files.get(root)
        if files is not None:
            return files

        config = self._config.model_copy(update={"kind": "local", "files_dir": root})
        files = StreamedFile(LocalStorageClient(config), self._policy)
        self._files[root] = files
        return files
