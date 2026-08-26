"""Загрузка вложения: тело идёт в хранилище потоком, а не через память или tmp."""

import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest
from chainlit.auth import get_current_user
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from boba.canvas.storage import StorageFullError
from boba.canvas.transfer import UploadPolicy
from boba.chainlit.data.storage import LocalStorageClient
from boba.chainlit.data.upload import UploadRoute
from boba.workspace.launcher import ReadWindow

pytestmark = pytest.mark.anyio

CHUNK = 64 * 1024


class FakeUser:
    """Пользователь сессии: роуту нужны только identifier и id."""

    def __init__(self, identifier: str = "ivanov", user_id: int = 7) -> None:
        self.identifier = identifier
        self.id = user_id


class FakeSession:
    """Сессия chainlit в объёме, который читает и пишет UploadRoute."""

    INSTANCES: ClassVar[dict[str, "FakeSession"]] = {}

    def __init__(self, session_id: str, files_dir: Path) -> None:
        self.id = session_id
        self.thread_id = "1f000000-0000-4000-8000-000000000abc"
        self.user = FakeUser()
        self.files: dict[str, dict[str, Any]] = {}
        self.files_spec: dict[str, Any] = {}
        self.files_dir = files_dir
        self.INSTANCES[session_id] = self


class MultipartBody:
    """Тело multipart, отдаваемое клиентом по кусочкам, как это делает браузер."""

    BOUNDARY = "boba-test-boundary"

    def __init__(self, filename: str, payload: bytes, mime: str) -> None:
        head = (
            f"--{self.BOUNDARY}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode()
        tail = f"\r\n--{self.BOUNDARY}--\r\n".encode()
        self._body = head + payload + tail

    @property
    def headers(self) -> dict[str, str]:
        return {"content-type": f"multipart/form-data; boundary={self.BOUNDARY}"}

    @property
    def size(self) -> int:
        return len(self._body)

    async def stream(self, chunk: int = 16 * 1024) -> AsyncIterator[bytes]:
        for start in range(0, len(self._body), chunk):
            yield self._body[start : start + chunk]


@pytest.fixture
def session(tmp_path: Path) -> FakeSession:
    FakeSession.INSTANCES.clear()
    return FakeSession("sess-1", tmp_path / "session-files")


@pytest.fixture
def app_builder(
    session: FakeSession,
    storage: LocalStorageClient,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[UploadPolicy], FastAPI]:
    from chainlit.session import WebsocketSession

    monkeypatch.setattr(
        WebsocketSession, "get_by_id", staticmethod(FakeSession.INSTANCES.get)
    )

    def build(policy: UploadPolicy) -> FastAPI:
        app = FastAPI()
        UploadRoute(storage, policy).install(app)
        app.dependency_overrides[get_current_user] = lambda: session.user
        return app

    return build


@pytest.fixture
def client_app(app_builder: Callable[[UploadPolicy], FastAPI]) -> FastAPI:
    return app_builder(UploadPolicy())


def transport(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


async def read_all(storage: LocalStorageClient, object_key: str) -> bytes:
    """Файл целиком: накапливает вызывающий, хранилище только стримит."""
    async with await storage.open_stream(object_key, ReadWindow.entire()) as body:
        collected = bytearray()
        async for chunk in body.chunks:
            collected.extend(chunk)

    return bytes(collected)


async def test_upload_lands_in_storage_and_is_served_back(
    client_app: FastAPI,
    session: FakeSession,
    storage: LocalStorageClient,
):
    payload = b"a" * (3 * CHUNK) + b"tail"

    async with transport(client_app) as client:
        response = await client.post(
            "/project/file",
            params={"session_id": session.id},
            files={"file": ("отчёт.bin", payload, "application/octet-stream")},
        )

    if response.status_code != 200:
        raise AssertionError(response.text)
    body = response.json()
    if body["name"] != "отчёт.bin":
        raise AssertionError('body["name"] == "отчёт.bin"')
    if body["size"] != len(payload):
        raise AssertionError('body["size"] == len(payload)')

    record = session.files[body["id"]]
    if await read_all(storage, record["object_key"]) != payload:
        raise AssertionError('await read_all(storage, record["object_key"]) == payload')

    async with transport(client_app) as client:
        served = await client.get(
            f"/project/file/{body['id']}", params={"session_id": session.id}
        )

    if served.status_code != 200:
        raise AssertionError("served.status_code == 200")
    if served.content != payload:
        raise AssertionError("served.content == payload")
    if served.headers["content-length"] != str(len(payload)):
        raise AssertionError('served.headers["content-length"] == str(len(payload))')
    if served.headers["accept-ranges"] != "bytes":
        raise AssertionError('served.headers["accept-ranges"] == "bytes"')


async def test_download_honors_range(
    client_app: FastAPI,
    session: FakeSession,
):
    payload = b"0123456789" * 100

    async with transport(client_app) as client:
        uploaded = await client.post(
            "/project/file",
            params={"session_id": session.id},
            files={"file": ("win.bin", payload, "application/octet-stream")},
        )
        file_id = uploaded.json()["id"]

        partial = await client.get(
            f"/project/file/{file_id}",
            params={"session_id": session.id},
            headers={"Range": "bytes=10-19"},
        )
        tail = await client.get(
            f"/project/file/{file_id}",
            params={"session_id": session.id},
            headers={"Range": "bytes=990-"},
        )
        suffix = await client.get(
            f"/project/file/{file_id}",
            params={"session_id": session.id},
            headers={"Range": "bytes=-5"},
        )

    if partial.status_code != 206:
        raise AssertionError("partial.status_code == 206")
    if partial.content != payload[10:20]:
        raise AssertionError("partial.content == payload[10:20]")
    if partial.headers["content-range"] != f"bytes 10-19/{len(payload)}":
        raise AssertionError('partial.headers["content-range"] == f"bytes 10-19/{len(…')
    if partial.headers["content-length"] != "10":
        raise AssertionError('partial.headers["content-length"] == "10"')

    if tail.status_code != 206:
        raise AssertionError("tail.status_code == 206")
    if tail.content != payload[990:]:
        raise AssertionError("tail.content == payload[990:]")

    if suffix.status_code != 206:
        raise AssertionError("suffix.status_code == 206")
    if suffix.content != payload[-5:]:
        raise AssertionError("suffix.content == payload[-5:]")


async def test_download_range_beyond_file_is_416(
    client_app: FastAPI,
    session: FakeSession,
):
    async with transport(client_app) as client:
        uploaded = await client.post(
            "/project/file",
            params={"session_id": session.id},
            files={"file": ("small.bin", b"abc", "application/octet-stream")},
        )
        file_id = uploaded.json()["id"]

        response = await client.get(
            f"/project/file/{file_id}",
            params={"session_id": session.id},
            headers={"Range": "bytes=100-"},
        )

    if response.status_code != 416:
        raise AssertionError("response.status_code == 416")
    if response.headers["content-range"] != "bytes */3":
        raise AssertionError('response.headers["content-range"] == "bytes */3"')


async def test_upload_never_buffers_the_body(
    client_app: FastAPI,
    session: FakeSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """Ни временного файла, ни единого куска размером с сам файл."""
    seen: list[int] = []
    original = LocalStorageClient.upload_stream

    async def spy(
        self: LocalStorageClient,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str = "application/octet-stream",
    ) -> dict[str, Any]:
        async def measured() -> AsyncIterator[bytes]:
            async for chunk in source:
                seen.append(len(chunk))
                yield chunk

        return await original(self, object_key, measured(), mime)

    monkeypatch.setattr(LocalStorageClient, "upload_stream", spy)

    spooled: list[int] = []
    real_spooled = tempfile.SpooledTemporaryFile

    def spy_spooled(*args: Any, **kwargs: Any) -> Any:
        spooled.append(1)
        return real_spooled(*args, **kwargs)

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", spy_spooled)

    payload = b"x" * (5 * CHUNK)
    body = MultipartBody("big.bin", payload, "application/octet-stream")

    async with transport(client_app) as client:
        response = await client.post(
            "/project/file",
            params={"session_id": session.id},
            headers=body.headers,
            content=body.stream(),
        )

    if response.status_code != 200:
        raise AssertionError(response.text)
    if spooled:
        raise AssertionError("тело не должно спуливаться во временный файл")
    if sum(seen) != len(payload):
        raise AssertionError("sum(seen) == len(payload)")
    if max(seen) >= len(payload):
        raise AssertionError("файл не должен приезжать одним куском")


async def test_rejected_upload_stops_reading_at_the_cap(
    app_builder: Callable[[UploadPolicy], FastAPI],
    session: FakeSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """Отвергнутый файл не должен заливаться целиком: вычитывание ограничено."""
    client_app = app_builder(UploadPolicy(drain_bytes=64 * 1024))

    async def full(
        self: LocalStorageClient,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str = "application/octet-stream",
    ) -> dict[str, Any]:
        async for _ in source:
            break
        raise StorageFullError("storage: no space left in the workspace image")

    monkeypatch.setattr(LocalStorageClient, "upload_stream", full)

    body = MultipartBody("big.bin", b"x" * (5 * CHUNK), "application/octet-stream")
    sent: list[int] = []

    async def counted() -> AsyncIterator[bytes]:
        async for chunk in body.stream():
            sent.append(len(chunk))
            yield chunk

    async with transport(client_app) as client:
        response = await client.post(
            "/project/file",
            params={"session_id": session.id},
            headers=body.headers,
            content=counted(),
        )

    if response.status_code != 507:
        raise AssertionError("response.status_code == 507")
    if sum(sent) >= body.size:
        raise AssertionError("чтение должно прекратиться на потолке")


async def test_rejected_upload_still_drains_the_body(
    client_app: FastAPI,
    session: FakeSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """В пределах потолка тело дочитывается, иначе клиент потеряет ответ."""

    async def full(
        self: LocalStorageClient,
        object_key: str,
        source: AsyncIterator[bytes],
        mime: str = "application/octet-stream",
    ) -> dict[str, Any]:
        async for _ in source:
            break
        raise StorageFullError("storage: no space left in the workspace image")

    monkeypatch.setattr(LocalStorageClient, "upload_stream", full)

    body = MultipartBody("big.bin", b"x" * (5 * CHUNK), "application/octet-stream")
    sent: list[int] = []

    async def counted() -> AsyncIterator[bytes]:
        async for chunk in body.stream():
            sent.append(len(chunk))
            yield chunk

    async with transport(client_app) as client:
        response = await client.post(
            "/project/file",
            params={"session_id": session.id},
            headers=body.headers,
            content=counted(),
        )

    if response.status_code != 507:
        raise AssertionError("response.status_code == 507")
    if "no space left" not in response.json()["detail"]:
        raise AssertionError('"no space left" in response.json()["detail"]')
    if sum(sent) != body.size:
        raise AssertionError("тело должно быть дочитано до конца")


async def test_form_fields_before_the_file_are_skipped(
    client_app: FastAPI,
    session: FakeSession,
    storage: LocalStorageClient,
):
    async with transport(client_app) as client:
        response = await client.post(
            "/project/file",
            params={"session_id": session.id},
            data={"kind": "attachment"},
            files={"file": ("note.txt", b"hello", "text/plain")},
        )

    if response.status_code != 200:
        raise AssertionError(response.text)
    record = session.files[response.json()["id"]]
    if await read_all(storage, record["object_key"]) != b"hello":
        raise AssertionError('await read_all(storage, record["object_key"]) == b"hell…')


async def test_body_without_a_file_part_is_rejected(
    client_app: FastAPI,
    session: FakeSession,
):
    async with transport(client_app) as client:
        response = await client.post(
            "/project/file",
            params={"session_id": session.id},
            data={"kind": "attachment"},
        )

    if response.status_code != 400:
        raise AssertionError("response.status_code == 400")


async def test_foreign_session_is_rejected(
    client_app: FastAPI,
    session: FakeSession,
):
    client_app.dependency_overrides[get_current_user] = lambda: FakeUser("petrov", 8)

    async with transport(client_app) as client:
        response = await client.post(
            "/project/file",
            params={"session_id": session.id},
            files={"file": ("note.txt", b"hello", "text/plain")},
        )

    if response.status_code != 401:
        raise AssertionError("response.status_code == 401")
