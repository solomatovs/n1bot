"""Файлы workspace пользователя на сервисе: потоковая запись и чтение по HTTP.

Байты файла идут отдельным маршрутом endpoint'а, а не вызовом MCP: тело
запроса читается чанками и сразу пишется в хранилище workspace, чтение
отдаёт окно чанков. Файл не оседает ни в памяти, ни на диске вне workspace.
Адрес файла — область (scope), каталог области и имя; владелец — вошедший
по токену, чужой workspace по адресу недостижим.

Ошибки:
HTTPException 400 — адрес не называет файл каталога области.
HTTPException 401 — запрос без токена входа либо токен негоден.
HTTPException 404 — файла нет в workspace.
HTTPException 416 — диапазон начинается за концом файла.
HTTPException 507 — в workspace не осталось места.
"""

from __future__ import annotations

import logging
import mimetypes
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any, ClassVar

import mcp_types as mt
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken
from fastmcp.tools import Tool
from fastmcp.tools.base import ToolResult
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from boba.canvas.journal import StreamJournalError, StreamKey
from boba.canvas.keys import ObjectKey, ThreadDir, WorkspaceMount
from boba.canvas.storage import StorageFullError, StorageNotFoundError
from boba.canvas.transfer import FileHeader, UploadPolicy
from boba.identity.context import Scope, Subject
from boba.mcp_server.auth import CallScopeError, CallScopes, TokenSubjects
from boba.runtime.served import StreamedFile
from boba.runtime.storage import (
    LocalStorageClient,
    LocalStorageConfig,
    StorageClient,
)
from boba.toolkit.channels import JournalChannels
from boba.toolkit.failure import ValidationText
from boba.toolkit.wire import FilesFeature
from boba.toolrun.streams import CallJournals

__all__ = ["FileRoutes", "FileUploadTool", "JournalRoutes", "RouteCallers"]

logger = logging.getLogger(__name__)


class FilePart(StrEnum):
    """Параметры пути маршрута файлов и заголовки ответа."""

    SCOPE = "scope"
    DIR = "dir"
    NAME = "name"
    ETAG = "etag"
    AUTHORIZATION = "authorization"
    BEARER = "bearer "
    FALLBACK_MIME = "application/octet-stream"


class FileStored(BaseModel):
    """Ответ записи файла: путь, каким его видят инструменты, и размер."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    size: int = Field(ge=0)


class RouteCallers:
    """Вошедший по токену запроса к маршруту сервиса.

    Создаётся сервером endpoint'а для маршрутов файлов и журналов. Токен
    запроса уже проверен входом fastmcp всего приложения endpoint'а: итог
    лежит в самом запросе (scope["user"]). Наличие токена и область
    endpoint'а вход на чужих маршрутах не требует, поэтому их сверяет
    маршрут: без токена — 401, без области endpoint'а — 403.
    """

    def __init__(self, subjects: TokenSubjects, endpoint: str) -> None:
        self._subjects = subjects
        self._endpoint = endpoint

    async def of(self, request: Request) -> Subject:
        # вошедший берётся из этого запроса: get_access_token() без токена в
        # запросе отдаёт вошедшего прошлого запроса того же соединения
        user = request.scope.get("user")
        if not isinstance(user, AuthenticatedUser):
            msg = (
                f"{request.method} {request.url.path}: expected a valid bearer "
                "token of the sign-in, got none"
            )
            raise HTTPException(status_code=401, detail=msg)

        access = user.access_token
        if not isinstance(access, AccessToken):
            msg = (
                f"{request.method} {request.url.path}: the sign-in of "
                f"{access.client_id!r} expects a token of this service, got "
                f"{type(access).__name__}"
            )
            raise HTTPException(status_code=401, detail=msg)

        if self._endpoint not in access.scopes:
            msg = (
                f"{request.method} {request.url.path}: mcp endpoint "
                f"{self._endpoint!r} is not granted to {access.client_id!r}, "
                f"the token carries scopes {sorted(access.scopes)}"
            )
            raise HTTPException(status_code=403, detail=msg)

        return self._subjects.of(access)


class JournalRoutes:
    """Журнал вызова целым файлом по маршруту endpoint'а.

    Создаётся сервером endpoint'а из журналов процесса. Окна журнала читает
    операция stream_read; скачать канал целиком — этот маршрут:
    `{base}/journals/{run}/{node}/{channel}`, потоком и с Range. Журнал
    отдаётся только тому, кто вызывал инструмент: чужой запуск — 404.
    """

    MIME: ClassVar[str] = "text/plain; charset=utf-8"

    def __init__(
        self,
        journals: CallJournals,
        callers: RouteCallers,
        config: LocalStorageConfig,
        base: str,
        published: str,
    ) -> None:
        self._journals = journals
        self._callers = callers
        self._config = config
        self._base = f"{base}/journals"
        self._published = f"{published}{self._base}"
        self._policy = UploadPolicy()
        self._files: dict[str, StreamedFile] = {}

    def path(self) -> str:
        """Путь маршрута журналов, каким его видит клиент (за прокси — с
        префиксом публикации): настройка расширения журнала."""
        return self._published

    def install(self, server: FastMCP) -> None:
        path = "/".join((self._base, "{run}", "{node}", "{channel}"))
        server.custom_route(path, methods=["GET"])(self.get)

    async def get(self, request: Request) -> Response:
        subject = await self._callers.of(request)
        params = request.path_params
        run = str(params["run"])
        node = str(params["node"])
        store = self._journals.store
        if store is None:
            msg = f"GET {request.url.path}: the stream journal is disabled"
            raise HTTPException(status_code=404, detail=msg)

        channel = JournalChannels.parse_visible(str(params["channel"]))
        if channel is None:
            visible = ", ".join(str(item) for item in JournalChannels.VISIBLE)
            msg = (
                f"GET {request.url.path}: channel {params['channel']!r} is not "
                f"downloadable, visible channels: {visible}"
            )
            raise HTTPException(status_code=404, detail=msg)

        try:
            key = StreamKey(user_id=subject.user_key, thread_id=run, call_id=node)
            root = store.vault_root(key.user_id)
        except (ValueError, StreamJournalError) as exc:
            msg = f"GET {request.url.path}: no journal of call {node!r}: {exc}"
            raise HTTPException(status_code=404, detail=msg) from exc

        rel_log = store.log_rel_path(key, channel)
        if rel_log is None:
            msg = (
                f"GET {request.url.path}: call {node!r} of run {run!r} has no "
                f"journal on channel {channel.value!r}"
            )
            raise HTTPException(status_code=404, detail=msg)

        disposition = f'attachment; filename="{key.call_id}.{channel}.log"'

        return await self._files_for(root).respond(
            rel_log,
            mime=self.MIME,
            range_header=request.headers.get("range", ""),
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


class FileRoutes:
    """Маршруты файлов workspace одного endpoint'а.

    Создаётся сервером endpoint'а (McpServer) из хранилища workspace и
    разбора вошедшего (RouteCallers); install() ставит маршруты в сервер
    fastmcp. settings() — настройки расширения FilesFeature, по которым
    клиент узнаёт адрес маршрута: путь с префиксом публикации published,
    под которым сервис стоит за прокси (без прокси — пустым).
    """

    def __init__(
        self,
        storage: StorageClient,
        callers: RouteCallers,
        base: str,
        published: str,
    ) -> None:
        self._storage = storage
        self._callers = callers
        self._base = f"{base}/files"
        self._published = f"{published}{self._base}"
        self._policy = UploadPolicy()
        self._files = StreamedFile(storage, self._policy)

    def settings(self, upload_tool: str) -> dict[str, Any]:
        return {
            FilesFeature.PATH.value: self._published,
            FilesFeature.UPLOAD.value: upload_tool,
            FilesFeature.WORKSPACE.value: WorkspaceMount.path(),
        }

    def address(self, scope: str, name: str) -> str:
        """Путь маршрута, куда клиент шлёт файл вложением области scope."""
        return "/".join((self._published, scope, ThreadDir.UPLOAD.value, name))

    def install(self, server: FastMCP) -> None:
        path = "/".join(
            (
                self._base,
                f"{{{FilePart.SCOPE.value}}}",
                f"{{{FilePart.DIR.value}}}",
                f"{{{FilePart.NAME.value}}}",
            )
        )
        # HEAD стоит раньше GET: маршрут GET у starlette отвечает и на HEAD
        server.custom_route(path, methods=["HEAD"])(self.head)
        server.custom_route(path, methods=["GET"])(self.get)
        server.custom_route(path, methods=["PUT"])(self.put)
        server.custom_route(path, methods=["DELETE"])(self.delete)

    async def put(self, request: Request) -> Response:
        """Запись файла потоком: чанки тела запроса идут прямо в хранилище."""
        key = await self._key(request)
        counted = CountedBody(request.stream())
        try:
            await self._storage.upload_stream(
                key.render(), counted.chunks(), self._mime(key)
            )
        except StorageFullError as exc:
            msg = f"PUT {request.url.path}: no space left in the workspace: {exc}"
            raise HTTPException(
                status_code=self._policy.no_space_status, detail=msg
            ) from exc

        logger.info("files: %s stored, %d bytes", key.render(), counted.size)
        stored = FileStored(path=key.in_workspace(), size=counted.size)

        return JSONResponse(stored.model_dump(mode="json"), status_code=201)

    async def get(self, request: Request) -> Response:
        """Чтение файла потоком; заголовок Range отвечает 206."""
        key = await self._key(request)

        return await self._files.respond(
            key.render(),
            mime=self._mime(key),
            range_header=request.headers.get("range", ""),
            content_disposition="",
        )

    async def head(self, request: Request) -> Response:
        """Размер и версия файла без тела: по ним клиент следит за правкой."""
        key = await self._key(request)
        try:
            stat = await self._storage.stat(key.render())
        except StorageNotFoundError as exc:
            msg = f"HEAD {request.url.path}: no such file in the workspace: {exc}"
            raise HTTPException(status_code=404, detail=msg) from exc

        headers = {
            FileHeader.CONTENT_LENGTH.value: str(stat.size),
            FileHeader.ACCEPT_RANGES.value: StreamedFile.ACCEPT_RANGES,
            FilePart.ETAG.value: f'"{stat.revision}"',
        }

        return Response(status_code=200, headers=headers, media_type=self._mime(key))

    async def delete(self, request: Request) -> Response:
        key = await self._key(request)
        if not await self._storage.delete_file(key.render()):
            msg = f"DELETE {request.url.path}: no such file in the workspace"
            raise HTTPException(status_code=404, detail=msg)

        return Response(status_code=204)

    async def _key(self, request: Request) -> ObjectKey:
        """Ключ файла в workspace вошедшего по адресу запроса."""
        subject = await self._callers.of(request)
        params = request.path_params
        try:
            scope = Scope.chat(str(params[FilePart.SCOPE.value]))

            return ObjectKey(
                user_id=subject.user_key,
                thread_id=scope.id,
                name=str(params[FilePart.NAME.value]),
                dir=ThreadDir(str(params[FilePart.DIR.value])),
            )
        except ValidationError as exc:
            msg = (
                f"{request.method} {request.url.path}: the address does not name "
                f"a file of a scope: {ValidationText.of(exc)}"
            )
            raise HTTPException(status_code=400, detail=msg) from exc
        except ValueError as exc:
            dirs = sorted(ThreadDir)
            msg = (
                f"{request.method} {request.url.path}: expected one of the scope "
                f"dirs {dirs}: {exc}"
            )
            raise HTTPException(status_code=400, detail=msg) from exc

    @staticmethod
    def _mime(key: ObjectKey) -> str:
        guessed = mimetypes.guess_type(key.name)[0]
        if not guessed:
            return FilePart.FALLBACK_MIME.value

        return guessed


class CountedBody:
    """Тело запроса чанками со счётом принятых байт. Создаётся маршрутом
    записи на один запрос; пустые чанки конца потока в хранилище не идут."""

    def __init__(self, source: AsyncIterator[bytes]) -> None:
        self._source = source
        self.size = 0

    async def chunks(self) -> AsyncIterator[bytes]:
        async for chunk in self._source:
            if not chunk:
                continue

            self.size += len(chunk)
            yield chunk


class FileUploadRequest(BaseModel):
    """Аргументы file_upload: имя файла в каталоге вложений области."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(
        min_length=1,
        description="File name as it should appear in the workspace upload dir.",
    )


class FileUploadTool(Tool):
    """Операция сервиса file_upload: адрес, по которому клиент загружает файл.

    Создаётся сервером endpoint'а рядом с FileRoutes. Байты файла вызовом
    MCP не передаются: модель получает адрес и способ отправки, а клиент
    шлёт файл потоком на маршрут файлов тем же токеном входа. Запуска
    области операция не открывает и в предел запусков не входит.
    """

    NAME: ClassVar[str] = "file_upload"

    DESCRIPTION: ClassVar[str] = (
        "Get the address to upload a file into the workspace:\n"
        "   - name — file name in the upload dir of the workspace\n"
        "The file body is sent by the client with HTTP PUT to the returned "
        "path of this server using the same bearer token; tools then read the "
        "file at the returned workspace path"
    )

    _routes: FileRoutes = PrivateAttr()
    _subjects: TokenSubjects = PrivateAttr()
    _scopes: CallScopes = PrivateAttr()

    def __init__(self, routes: FileRoutes, subjects: TokenSubjects) -> None:
        super().__init__(
            name=self.NAME,
            description=self.DESCRIPTION,
            parameters=FileUploadRequest.model_json_schema(),
        )
        self._routes = routes
        self._subjects = subjects
        self._scopes = CallScopes()

    async def run(self, arguments: dict[str, object]) -> ToolResult:
        try:
            request = FileUploadRequest.model_validate(arguments)
            subject = self._subjects.current()
            scope = self._scopes.of(subject)
            key = ObjectKey(
                user_id=subject.user_key, thread_id=scope.id, name=request.name
            )
        except ValidationError as exc:
            return self._refused(
                f"file_upload: the arguments do not name a file: "
                f"{ValidationText.of(exc)}"
            )
        except CallScopeError as exc:
            return self._refused(f"file_upload: {exc}")

        address = self._routes.address(key.thread_id, key.name)
        text = (
            f"upload the file body with HTTP PUT to {address} of this server "
            f"(same bearer token); tools read it at {key.in_workspace()}"
        )

        return ToolResult(
            content=[mt.TextContent(type="text", text=text)],
            structured_content={
                "method": "PUT",
                "path": address,
                "workspace_path": key.in_workspace(),
            },
        )

    @staticmethod
    def _refused(message: str) -> ToolResult:
        return ToolResult(
            content=[mt.TextContent(type="text", text=message)], is_error=True
        )
