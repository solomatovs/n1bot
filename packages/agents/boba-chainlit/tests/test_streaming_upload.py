"""Загрузка вложения: тело идёт в хранилище потоком, а не через память или tmp."""

import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from chainlit.auth import get_current_user
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from boba.chainlit.chat.data.storage import LocalStorageClient, StorageFullError
from boba.chainlit.chat.data.upload import UploadRoute

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
def client_app(
    session: FakeSession,
    storage: LocalStorageClient,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    from chainlit.session import WebsocketSession

    monkeypatch.setattr(
        WebsocketSession, "get_by_id", staticmethod(FakeSession.INSTANCES.get)
    )
    app = FastAPI()
    UploadRoute(storage).install(app)
    app.dependency_overrides[get_current_user] = lambda: session.user
    return app


def transport(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


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

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "отчёт.bin"
    assert body["size"] == len(payload)

    record = session.files[body["id"]]
    assert await storage.read_file(record["object_key"]) == payload

    async with transport(client_app) as client:
        served = await client.get(
            f"/project/file/{body['id']}", params={"session_id": session.id}
        )

    assert served.status_code == 200
    assert served.content == payload


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

    assert response.status_code == 200, response.text
    assert not spooled, "тело не должно спуливаться во временный файл"
    assert sum(seen) == len(payload)
    assert max(seen) < len(payload), "файл не должен приезжать одним куском"


async def test_rejected_upload_stops_reading_at_the_cap(
    client_app: FastAPI,
    session: FakeSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """Отвергнутый файл не должен заливаться целиком: вычитывание ограничено."""
    monkeypatch.setattr(UploadRoute, "DRAIN_BYTES", 64 * 1024)

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

    assert response.status_code == 507
    assert sum(sent) < body.size, "чтение должно прекратиться на потолке"


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

    assert response.status_code == 507
    assert "no space left" in response.json()["detail"]
    assert sum(sent) == body.size, "тело должно быть дочитано до конца"


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

    assert response.status_code == 200, response.text
    record = session.files[response.json()["id"]]
    assert await storage.read_file(record["object_key"]) == b"hello"


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

    assert response.status_code == 400


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

    assert response.status_code == 401
