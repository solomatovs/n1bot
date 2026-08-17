"""Отдача вложения по HTTP на реальных данных: сокет, окна Range, расход памяти.

Сервер поднимается настоящим uvicorn на локальном порту: ASGITransport httpx
собирает тело ответа целиком, поэтому потоковость через него не проверить.
Данные — детерминированный паттерн, байт по позиции i равен i % 256.
"""

import asyncio
import hashlib
import logging
import tracemalloc
from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar, Self

import pytest
import uvicorn
from chainlit.auth import get_current_user
from conftest import FakeUrl
from fastapi import FastAPI
from httpx import AsyncClient

from boba.chainlit.data.storage import LocalStorageClient
from boba.chainlit.data.upload import SessionFiles, UploadPolicy, UploadRoute
from boba.chainlit.domain.keys import ObjectKey

pytestmark = pytest.mark.anyio

THREAD_ID = "1f000000-0000-4000-8000-000000000abc"
USER_ID = 7
FILE_NAME = "big.bin"
UPLOAD_LOGGER = "boba.chainlit.data.upload"


class PatternPayload:
    """Детерминированные данные: байт по позиции i равен i % 256."""

    PERIOD: ClassVar[int] = 256
    BLOCK_BYTES: ClassVar[int] = 1 << 20

    def __init__(self, blocks: int) -> None:
        self._blocks = blocks
        self._block = bytes(range(self.PERIOD)) * (self.BLOCK_BYTES // self.PERIOD)

    @property
    def size(self) -> int:
        return self._blocks * self.BLOCK_BYTES

    async def source(self) -> AsyncIterator[bytes]:
        for _ in range(self._blocks):
            yield self._block

    def digest(self) -> str:
        state = hashlib.sha256()
        for _ in range(self._blocks):
            state.update(self._block)

        return state.hexdigest()

    @classmethod
    def slice_at(cls, offset: int, length: int) -> bytes:
        stop = offset + length
        return bytes(i % cls.PERIOD for i in range(offset, stop))


class MemoryProbe:
    """Пиковая память процесса за время блока."""

    def __init__(self) -> None:
        self.peak_bytes = 0

    def __enter__(self) -> Self:
        tracemalloc.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.peak_bytes = peak


class LiveServer:
    """uvicorn на свободном порту в текущем цикле событий."""

    STARTUP_POLL_SEC: ClassVar[float] = 0.02

    def __init__(self, app: FastAPI) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        self._server = uvicorn.Server(config)
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._server.serve())

        while not self._server.started:
            if self._task.done():
                self._task.result()
                raise RuntimeError("uvicorn stopped before it started")

            await asyncio.sleep(self.STARTUP_POLL_SEC)

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await self._task

    @property
    def base_url(self) -> str:
        port = self._server.servers[0].sockets[0].getsockname()[1]
        return FakeUrl.loopback(port)


class ServedUser:
    """Пользователь сессии: роуту нужны только identifier и id."""

    def __init__(self) -> None:
        self.identifier = "ivanov"
        self.id = USER_ID


class ServedSession:
    """Сессия chainlit в объёме, который читает роут скачивания."""

    INSTANCES: ClassVar[dict[str, "ServedSession"]] = {}

    def __init__(self, session_id: str, files_dir: Path) -> None:
        self.id = session_id
        self.thread_id = THREAD_ID
        self.user = ServedUser()
        self.files: dict[str, dict[str, Any]] = {}
        self.files_spec: dict[str, Any] = {}
        self.files_dir = files_dir
        self.INSTANCES[session_id] = self


@pytest.fixture
def payload() -> PatternPayload:
    return PatternPayload(blocks=64)


@pytest.fixture
async def served(
    storage: LocalStorageClient,
    payload: PatternPayload,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FastAPI, ServedSession, str]:
    """Файл уже в хранилище и зарегистрирован в сессии: остаётся его отдать."""
    from chainlit.session import WebsocketSession

    ServedSession.INSTANCES.clear()
    session = ServedSession("sess-stream", tmp_path / "session-files")
    monkeypatch.setattr(
        WebsocketSession, "get_by_id", staticmethod(ServedSession.INSTANCES.get)
    )

    key = ObjectKey.build(USER_ID, THREAD_ID, FILE_NAME, "el-1")
    await storage.upload_stream(key.render(), payload.source())

    file_id = SessionFiles.register(
        session, key, mime="application/octet-stream", size=payload.size
    )

    app = FastAPI()
    UploadRoute(storage, UploadPolicy()).install(app)
    app.dependency_overrides[get_current_user] = lambda: session.user

    return app, session, file_id


class TestServedStreaming:
    """HTTP-отдача: тело течёт, память не зависит от размера файла."""

    PEAK_LIMIT: ClassVar[int] = 16 << 20
    """Потолок пика на клиента и сервер вместе: чанк 1 МиБ плюс запас."""

    CLIENT_CHUNK: ClassVar[int] = 64 * 1024

    async def test_whole_file_streams_over_the_socket(
        self,
        served: tuple[FastAPI, ServedSession, str],
        payload: PatternPayload,
    ) -> None:
        """64 МиБ через реальный сокет: хеш сходится, память остаётся малой."""
        app, session, file_id = served

        async with (
            LiveServer(app) as server,
            AsyncClient(base_url=server.base_url) as client,
        ):
            with MemoryProbe() as probe:
                state = hashlib.sha256()
                received = 0
                async with client.stream(
                    "GET",
                    f"/project/file/{file_id}",
                    params={"session_id": session.id},
                ) as response:
                    if response.status_code != 200:
                        raise AssertionError("response.status_code == 200")
                    length = response.headers["content-length"]
                    ranges = response.headers["accept-ranges"]

                    async for chunk in response.aiter_bytes(self.CLIENT_CHUNK):
                        received += len(chunk)
                        state.update(chunk)

        if length != str(payload.size):
            raise AssertionError("length == str(payload.size)")
        if ranges != "bytes":
            raise AssertionError('ranges == "bytes"')
        if received != payload.size:
            raise AssertionError("received == payload.size")
        if state.hexdigest() != payload.digest():
            raise AssertionError("state.hexdigest() == payload.digest()")
        if probe.peak_bytes >= self.PEAK_LIMIT:
            raise AssertionError("probe.peak_bytes < self.PEAK_LIMIT")

    async def test_range_window_is_served_from_the_middle(
        self,
        served: tuple[FastAPI, ServedSession, str],
        payload: PatternPayload,
    ) -> None:
        """Окно из середины большого файла: отдаётся только оно."""
        app, session, file_id = served
        offset = 50 * PatternPayload.BLOCK_BYTES + 11
        length = 8192
        last = offset + length - 1

        async with (
            LiveServer(app) as server,
            AsyncClient(base_url=server.base_url) as client,
        ):
            with MemoryProbe() as probe:
                response = await client.get(
                    f"/project/file/{file_id}",
                    params={"session_id": session.id},
                    headers={"Range": f"bytes={offset}-{last}"},
                )

        if response.status_code != 206:
            raise AssertionError("response.status_code == 206")
        if response.headers["content-range"] != f"bytes {offset}-{last}/{payload.size}":
            raise AssertionError('response.headers["content-range"] == f"bytes {offse…')
        if response.headers["content-length"] != str(length):
            raise AssertionError('response.headers["content-length"] == str(length)')
        if response.content != PatternPayload.slice_at(offset, length):
            raise AssertionError("response.content == PatternPayload.slice_at(offset,…")
        if probe.peak_bytes >= self.PEAK_LIMIT:
            raise AssertionError("probe.peak_bytes < self.PEAK_LIMIT")

    async def test_client_may_abandon_the_stream(
        self,
        served: tuple[FastAPI, ServedSession, str],
        payload: PatternPayload,
    ) -> None:
        """Клиент ушёл после первых байт: сервер не досылает остальное."""
        app, session, file_id = served

        async with (
            LiveServer(app) as server,
            AsyncClient(base_url=server.base_url) as client,
        ):
            received = 0
            async with client.stream(
                "GET",
                f"/project/file/{file_id}",
                params={"session_id": session.id},
            ) as response:
                if response.status_code != 200:
                    raise AssertionError("response.status_code == 200")
                async for chunk in response.aiter_bytes(self.CLIENT_CHUNK):
                    received += len(chunk)
                    break

        if not (0 < received < payload.size):
            raise AssertionError("0 < received < payload.size")

    async def test_log_reports_start_progress_and_finish(
        self,
        served: tuple[FastAPI, ServedSession, str],
        payload: PatternPayload,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Лог отдачи: строка до первого байта, отметки по ходу и итог."""
        app, session, file_id = served

        with caplog.at_level(logging.INFO, logger=UPLOAD_LOGGER):
            async with (
                LiveServer(app) as server,
                AsyncClient(base_url=server.base_url) as client,
            ):
                async with client.stream(
                    "GET",
                    f"/project/file/{file_id}",
                    params={"session_id": session.id},
                ) as response:
                    if response.status_code != 200:
                        raise AssertionError("response.status_code == 200")
                    async for _ in response.aiter_bytes(self.CLIENT_CHUNK):
                        continue

        lines = [record.message for record in caplog.records]
        started = [line for line in lines if "sending" in line]
        progress = [line for line in lines if "streaming" in line]
        finished = [line for line in lines if "sent," in line]

        if not (started):
            raise AssertionError(lines)
        if not (progress):
            raise AssertionError("не видно хода передачи")
        if not (finished):
            raise AssertionError(lines)
        if self._mib(payload.size) not in finished[0]:
            raise AssertionError("self._mib(payload.size) in finished[0]")

    async def test_log_marks_the_client_abort(
        self,
        served: tuple[FastAPI, ServedSession, str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Клиент ушёл на первом чанке: недоотданное тело видно в логе."""
        app, session, file_id = served

        with caplog.at_level(logging.INFO, logger=UPLOAD_LOGGER):
            async with (
                LiveServer(app) as server,
                AsyncClient(base_url=server.base_url) as client,
                client.stream(
                    "GET",
                    f"/project/file/{file_id}",
                    params={"session_id": session.id},
                ) as response,
            ):
                async for _ in response.aiter_bytes(self.CLIENT_CHUNK):
                    break

            await asyncio.sleep(0.2)

        aborted = [r.message for r in caplog.records if "aborted" in r.message]
        if not (aborted):
            raise AssertionError([r.message for r in caplog.records])

    @staticmethod
    def _mib(size: int) -> str:
        return f"{size / (1024 * 1024):.1f} MiB"

    async def test_windows_walk_the_file_by_range(
        self,
        served: tuple[FastAPI, ServedSession, str],
    ) -> None:
        """Прокрутка бегунка вьювером: окна запрашиваются вразнобой."""
        app, session, file_id = served
        length = 4096
        offsets = [
            0,
            32 * PatternPayload.BLOCK_BYTES + 5,
            63 * PatternPayload.BLOCK_BYTES,
            7 * PatternPayload.BLOCK_BYTES + 3,
        ]

        async with (
            LiveServer(app) as server,
            AsyncClient(base_url=server.base_url) as client,
        ):
            for offset in offsets:
                last = offset + length - 1
                response = await client.get(
                    f"/project/file/{file_id}",
                    params={"session_id": session.id},
                    headers={"Range": f"bytes={offset}-{last}"},
                )

                if response.status_code != 206:
                    raise AssertionError(f"offset {offset}")
                expected = PatternPayload.slice_at(offset, length)
                if response.content != expected:
                    raise AssertionError(f"offset {offset}")
