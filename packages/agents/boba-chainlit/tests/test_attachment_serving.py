"""Отдача вложений: mime из таблицы elements обязан доехать до ответа."""

import json

import chainlit as cl
import plotly.graph_objects as go
import pytest
from chainlit.auth import get_current_user
from chainlit.user import PersistedUser
from conftest import Seed
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from boba.chainlit.chat.data import data_layer as data_layer_module
from boba.chainlit.chat.data.attachment_url import AttachmentUrl
from boba.chainlit.chat.data.serving import AttachmentServing
from boba.chainlit.chat.data.storage import LocalStorageClient

pytestmark = pytest.mark.anyio

CHART_NAME = "EURUSD — выдуманные котировки (свечной график)"


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
    monkeypatch.setattr(data_layer_module, "current_user_id", lambda: seeded.user.id)
    element = await create_chart_element(seeded)

    url = AttachmentUrl(thread_id=seeded.thread_id, element_id=element.id)

    thread = await layer.get_thread(seeded.thread_id)
    assert thread is not None
    stored = next(e for e in thread["elements"] if e["id"] == element.id)
    assert stored["url"] is not None
    assert stored["url"].endswith(url.path())

    app = build_serving_app(AttachmentServing(storage, lambda: layer), seeded.user)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://boba") as client:
        response = await client.get(url.path())

    assert response.status_code == 200
    # только с таким content-type фронт парсит тело как JSON и рисует график
    assert response.headers["content-type"].startswith("application/json")
    figure_spec = json.loads(response.content)
    assert figure_spec["data"][0]["type"] == "candlestick"


async def test_foreign_user_gets_no_file(
    seeded: Seed,
    storage: LocalStorageClient,
    monkeypatch: pytest.MonkeyPatch,
):
    layer = seeded.layer
    monkeypatch.setattr(data_layer_module, "current_user_id", lambda: seeded.user.id)
    element = await create_chart_element(seeded)

    stranger = PersistedUser(
        id="999", identifier="stranger", createdAt=seeded.user.createdAt
    )
    app = build_serving_app(AttachmentServing(storage, lambda: layer), stranger)
    url = AttachmentUrl(thread_id=seeded.thread_id, element_id=element.id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://boba") as client:
        response = await client.get(url.path())

    assert response.status_code == 404
