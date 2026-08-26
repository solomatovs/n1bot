"""Отдача вложений: mime из таблицы elements обязан доехать до ответа."""

import json
from pathlib import Path

import chainlit as cl
import plotly.graph_objects as go
import pytest
from chainlit.auth import get_current_user
from chainlit.user import PersistedUser
from conftest import FakeUrl, Seed, use_session
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from boba.canvas.keys import ElementProps, ObjectKey, ThreadDir
from boba.canvas.transfer import UploadPolicy
from boba.chainlit.agent.tools.send_file import WorkspaceFile
from boba.chainlit.data.storage import LocalStorageClient
from boba.chainlit.data.upload import AttachmentServing
from boba.chainlit.domain.keys import AttachmentUrl
from boba.chainlit.rendering.chat_view import ChatView, StepRole

pytestmark = pytest.mark.anyio

CHART_NAME = "EURUSD — выдуманные котировки (свечной график)"
REPORT_NAME = "report.txt"
REPORT_BODY = b"quarterly report body"


def build_serving_app(serving: AttachmentServing, user: PersistedUser) -> FastAPI:
    app = FastAPI()
    app.add_api_route(AttachmentUrl.ROUTE, serving.serve, methods=["GET"])
    app.dependency_overrides[get_current_user] = lambda: user
    return app


async def create_chart_element(seeded: Seed) -> cl.Plotly:
    figure = go.Figure(
        data=[
            go.Candlestick(
                x=["2024-01-01", "2024-01-02"],
                open=[1.0850, 1.0872],
                high=[1.0885, 1.0895],
                low=[1.0830, 1.0840],
                close=[1.0872, 1.0845],
            )
        ]
    )
    element = cl.Plotly(
        thread_id=seeded.thread_id,
        for_id=seeded.answer_step_id,
        name=CHART_NAME,
        figure=figure,
        display="inline",
    )
    await seeded.layer.create_element(element)
    return element


async def test_persisted_plotly_chart_is_served_as_json(
    seeded: Seed,
    storage: LocalStorageClient,
    monkeypatch: pytest.MonkeyPatch,
):
    layer = seeded.layer
    use_session(monkeypatch, user_id=seeded.user.id)
    element = await create_chart_element(seeded)

    url = AttachmentUrl(
        thread_id=seeded.thread_id,
        dir=ThreadDir.UPLOAD,
        element_id=element.id,
    )

    thread = await layer.get_thread(seeded.thread_id)
    if thread is None:
        raise AssertionError("thread is not None")
    elements = thread["elements"]
    if elements is None:
        raise AssertionError("elements is not None")
    stored = next(e for e in elements if e.get("id") == element.id)
    stored_url = stored.get("url")
    if stored_url is None:
        raise AssertionError("stored_url is not None")
    if not (stored_url.endswith(url.path())):
        raise AssertionError("stored_url.endswith(url.path())")

    app = build_serving_app(
        AttachmentServing(storage, lambda: layer, UploadPolicy()), seeded.user
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=FakeUrl.BASE) as client:
        response = await client.get(url.path())

    if response.status_code != 200:
        raise AssertionError("response.status_code == 200")
    # только с таким content-type фронт парсит тело как JSON и рисует график
    if not (response.headers["content-type"].startswith("application/json")):
        raise AssertionError('response.headers["content-type"].startswith("applicatio…')
    figure_spec = json.loads(response.content)
    if figure_spec["data"][0]["type"] != "candlestick":
        raise AssertionError('figure_spec["data"][0]["type"] == "candlestick"')


async def test_bot_file_is_shown_without_copying(
    seeded: Seed,
    storage: LocalStorageClient,
    files_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Тул send_file заводит элемент на готовый файл: содержимое не копируется."""
    layer = seeded.layer
    use_session(monkeypatch, user_id=seeded.user.id)

    # файл уже в каталоге вложений треда — его туда положил агент через bash
    key = ObjectKey.build(seeded.user.id, seeded.thread_id, REPORT_NAME, "el")
    await storage.upload_file(
        object_key=key.render(), data=REPORT_BODY, mime="text/plain"
    )
    stored_before = sorted(p.name for p in files_dir.rglob("*") if p.is_file())

    element_id = ChatView.derive_id(seeded.thread_id, "call_1", StepRole.ELEMENT)
    if element_id is None:
        raise AssertionError("element_id is not None")
    element = cl.File(
        id=element_id,
        name=key.name,
        thread_id=seeded.thread_id,
        for_id=seeded.answer_step_id,
        url=layer.links.url(seeded.thread_id, element_id, ThreadDir.UPLOAD),
        mime="text/plain",
        display="inline",
    )
    await layer.create_element(element)

    if sorted(p.name for p in files_dir.rglob("*") if p.is_file()) != stored_before:
        raise AssertionError('sorted(p.name for p in files_dir.rglob("*") if p.is_fil…')

    thread = await layer.get_thread(seeded.thread_id)
    if thread is None:
        raise AssertionError("thread is not None")
    elements = thread["elements"]
    if elements is None:
        raise AssertionError("elements is not None")
    shown = next(e for e in elements if e.get("id") == element_id)
    url = AttachmentUrl(
        thread_id=seeded.thread_id,
        dir=ThreadDir.UPLOAD,
        element_id=element_id,
    )
    stored_url = shown.get("url")
    if stored_url is None:
        raise AssertionError("stored_url is not None")
    if not (stored_url.endswith(url.path())):
        raise AssertionError("stored_url.endswith(url.path())")

    app = build_serving_app(
        AttachmentServing(storage, lambda: layer, UploadPolicy()), seeded.user
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=FakeUrl.BASE) as client:
        response = await client.get(url.path())

    if response.status_code != 200:
        raise AssertionError("response.status_code == 200")
    if response.content != REPORT_BODY:
        raise AssertionError("response.content == REPORT_BODY")
    if not (response.headers["content-type"].startswith("text/plain")):
        raise AssertionError('response.headers["content-type"].startswith("text/plain…')
    if response.headers["content-length"] != str(len(REPORT_BODY)):
        raise AssertionError('response.headers["content-length"] == str(len(REPORT_BO…')
    if response.headers["accept-ranges"] != "bytes":
        raise AssertionError('response.headers["accept-ranges"] == "bytes"')


async def test_attachment_range_is_served_partially(
    seeded: Seed,
    storage: LocalStorageClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Range — транспорт оконного чтения: вьюверы канваса тянут файл кусками."""
    layer = seeded.layer
    use_session(monkeypatch, user_id=seeded.user.id)

    key = ObjectKey.build(seeded.user.id, seeded.thread_id, REPORT_NAME, "el")
    await storage.upload_file(
        object_key=key.render(), data=REPORT_BODY, mime="text/plain"
    )

    element_id = ChatView.derive_id(seeded.thread_id, "call_rng", StepRole.ELEMENT)
    if element_id is None:
        raise AssertionError("element_id is not None")
    element = cl.File(
        id=element_id,
        name=key.name,
        thread_id=seeded.thread_id,
        for_id=seeded.answer_step_id,
        url=layer.links.url(seeded.thread_id, element_id, ThreadDir.UPLOAD),
        mime="text/plain",
        display="inline",
    )
    await layer.create_element(element)

    url = AttachmentUrl(
        thread_id=seeded.thread_id,
        dir=ThreadDir.UPLOAD,
        element_id=element_id,
    )
    app = build_serving_app(
        AttachmentServing(storage, lambda: layer, UploadPolicy()), seeded.user
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=FakeUrl.BASE) as client:
        partial = await client.get(url.path(), headers={"Range": "bytes=0-8"})
        beyond = await client.get(url.path(), headers={"Range": "bytes=9999-"})

    if partial.status_code != 206:
        raise AssertionError("partial.status_code == 206")
    if partial.content != REPORT_BODY[:9]:
        raise AssertionError("partial.content == REPORT_BODY[:9]")
    if partial.headers["content-range"] != f"bytes 0-8/{len(REPORT_BODY)}":
        raise AssertionError('partial.headers["content-range"] == f"bytes 0-8/{len(RE…')

    if beyond.status_code != 416:
        raise AssertionError("beyond.status_code == 416")
    if beyond.headers["content-range"] != f"bytes */{len(REPORT_BODY)}":
        raise AssertionError('beyond.headers["content-range"] == f"bytes */{len(REPOR…')


async def test_foreign_user_gets_no_file(
    seeded: Seed,
    storage: LocalStorageClient,
    monkeypatch: pytest.MonkeyPatch,
):
    layer = seeded.layer
    use_session(monkeypatch, user_id=seeded.user.id)
    element = await create_chart_element(seeded)

    stranger = PersistedUser(
        id="999", identifier="stranger", createdAt=seeded.user.createdAt
    )
    app = build_serving_app(
        AttachmentServing(storage, lambda: layer, UploadPolicy()), stranger
    )
    url = AttachmentUrl(
        thread_id=seeded.thread_id,
        dir=ThreadDir.UPLOAD,
        element_id=element.id,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=FakeUrl.BASE) as client:
        response = await client.get(url.path())

    if response.status_code != 404:
        raise AssertionError("response.status_code == 404")


async def test_diagram_from_mermaid_dir_is_served(
    seeded: Seed,
    storage: LocalStorageClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Файл из mermaid/ отдаётся по своей ссылке: раньше отдача звала upload/."""
    layer = seeded.layer
    use_session(monkeypatch, user_id=seeded.user.id)
    key = ObjectKey.build(
        seeded.user.id,
        seeded.thread_id,
        "ndfl.mmd",
        "el",
        dir_thread=ThreadDir.MERMAID,
    )
    await storage.upload_file(
        object_key=key.render(), data="flowchart LR\n  A --> B\n", mime="text/plain"
    )

    element_id = ChatView.derive_id(seeded.thread_id, "call_diagram", StepRole.ELEMENT)
    if element_id is None:
        raise AssertionError("element_id is not None")
    element = WorkspaceFile(
        id=element_id,
        name=key.name,
        thread_id=seeded.thread_id,
        for_id=seeded.answer_step_id,
        url=layer.links.url(seeded.thread_id, element_id, key.dir),
        mime="text/plain",
        display="inline",
        props=ElementProps(dir=key.dir).model_dump(mode="json"),
    )
    await layer.create_element(element)

    thread = await layer.get_thread(seeded.thread_id)
    if thread is None:
        raise AssertionError("thread is not None")
    elements = thread["elements"]
    if elements is None:
        raise AssertionError("elements is not None")
    shown = next(e for e in elements if e.get("id") == element_id)
    stored_url = shown.get("url")
    if stored_url is None:
        raise AssertionError("stored_url is not None")
    if not (
        stored_url.endswith(f"/attachment/{seeded.thread_id}/mermaid/{element_id}")
    ):
        raise AssertionError('stored_url.endswith(f"/attachment/{seeded.thread_id}/me…')

    app = build_serving_app(
        AttachmentServing(storage, lambda: layer, UploadPolicy()), seeded.user
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=FakeUrl.BASE) as client:
        response = await client.get(
            AttachmentUrl(
                thread_id=seeded.thread_id,
                dir=ThreadDir.MERMAID,
                element_id=element_id,
            ).path()
        )

    if response.status_code != 200:
        raise AssertionError("response.status_code == 200")
    if not (response.content.decode().startswith("flowchart LR")):
        raise AssertionError('response.content.decode().startswith("flowchart LR")')
