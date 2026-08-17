"""Действия фронта по закрытой сессии: гонка ухода со страницы, не сбой.

Вкладку закрывают, сокет умирает, а последний POST /project/action уже в
пути — chainlit ищет websocket-сессию и падает ValueError до колбэка.
Проверяется весь http-стек приложения: middleware поверх роута действий и
настоящий реестр сессий chainlit.
"""

from typing import Any

import pytest
from chainlit.session import WebsocketSession
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from boba.chainlit.infra.stale_action import StaleActionMiddleware

pytestmark = pytest.mark.anyio

SESSION_ID = "session-alive-1"
SOCKET_ID = "socket-alive-1"
ACTION = {
    "name": "canvas_leave",
    "payload": {"path": "stream://call-1/tool_stdout", "nonce": "n-1"},
    "label": "",
    "tooltip": "",
    "icon": None,
    "forId": None,
    "id": "e2e",
}


class ActionProbe:
    """Роут действий: считает вызовы, дошедшие до обработчика."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = payload["sessionId"]
        self.calls.append(session)

        return {"success": True, "session": session}


def build_app(probe: ActionProbe) -> FastAPI:
    app = FastAPI()
    app.add_api_route(
        StaleActionMiddleware.PATH, probe.call_action, methods=["POST"]
    )
    app.add_middleware(StaleActionMiddleware)

    return app


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    "отсеву действий http-контекст chainlit не нужен"


@pytest.fixture
async def live_session() -> Any:
    """Живая websocket-сессия в реестре chainlit; убирается за собой."""

    def emit(event: str, data: Any) -> None:
        del event, data

    async def emit_call(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    session = WebsocketSession(
        id=SESSION_ID,
        socket_id=SOCKET_ID,
        emit=emit,
        emit_call=emit_call,
        user_env={},
        client_type="webapp",
    )
    yield session
    await session.delete()

    if WebsocketSession.get_by_id(SESSION_ID) is not None:
        raise AssertionError("сессия теста осталась в реестре chainlit")


async def _post(app: FastAPI, session_id: str) -> Any:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            StaleActionMiddleware.PATH,
            json={"sessionId": session_id, "action": ACTION},
        )


async def test_action_of_a_live_session_reaches_the_handler(live_session: Any) -> None:
    """Обычный путь: сессия жива, тело доезжает до обработчика целиком."""
    del live_session
    probe = ActionProbe()

    answer = await _post(build_app(probe), SESSION_ID)

    if answer.status_code != 200:
        raise AssertionError("answer.status_code == 200")
    if answer.json()["session"] != SESSION_ID:
        raise AssertionError('answer.json()["session"] == SESSION_ID')
    if probe.calls != [SESSION_ID]:
        raise AssertionError("probe.calls == [SESSION_ID]")


async def test_action_of_a_gone_session_is_refused_without_the_handler() -> None:
    """Сессии нет: ответ 409, обработчик не зовётся, ошибки наверх не идёт."""
    probe = ActionProbe()

    answer = await _post(build_app(probe), "session-gone-1")

    if answer.status_code != StaleActionMiddleware.STATUS:
        raise AssertionError("answer.status_code == StaleActionMiddleware.STATUS")
    if answer.json()["detail"] != "session is gone":
        raise AssertionError('answer.json()["detail"] == "session is gone"')
    if probe.calls:
        raise AssertionError("not probe.calls")


async def test_body_outside_the_contract_goes_to_the_handler() -> None:
    """Тело не по контракту судит chainlit, а не отсев: запрос идёт дальше."""
    probe = ActionProbe()
    app = build_app(probe)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        answer = await client.post(StaleActionMiddleware.PATH, content=b"{ not json")

    if answer.status_code != 422:
        raise AssertionError("answer.status_code == 422")
    if probe.calls:
        raise AssertionError("not probe.calls")


async def test_other_routes_are_not_touched(live_session: Any) -> None:
    """Отсев смотрит только на действия: прочие запросы проходят как есть."""
    del live_session
    probe = ActionProbe()
    app = build_app(probe)

    async def user() -> dict[str, str]:
        return {"identifier": "solomatovs"}

    app.add_api_route("/user", user, methods=["GET"])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        answer = await client.get("/user")

    if answer.status_code != 200:
        raise AssertionError("answer.status_code == 200")
