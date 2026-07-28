from typing import ClassVar

import httpx
import pytest
from test_hermes_events import TURN

from boba.hermes.agent.api import HermesApiClient
from boba.hermes.chat import turn as turn_module
from boba.hermes.chat.turn import HermesTurn
from boba.hermes.errors import AgentError
from boba.hermes.infra.config import HermesConfig

pytestmark = pytest.mark.anyio

PROFILE = "solomatovs"
SESSION = "6f1c0e3c-0000-4000-8000-000000000001"


class FakeMessage:
    """cl.Message без сокета: копит текст так же, как это делает chainlit."""

    sent: ClassVar[list["FakeMessage"]] = []

    def __init__(self, content: str = "") -> None:
        self.content = content
        self.streamed = 0
        FakeMessage.sent.append(self)

    async def stream_token(self, token: str) -> None:
        self.content += token
        self.streamed += 1

    async def send(self) -> None:
        pass


class FakeStep:
    """cl.Step без сокета: помнит, чем закончился вызов инструмента."""

    created: ClassVar[list["FakeStep"]] = []

    def __init__(self, name: str = "", type: str = "") -> None:  # noqa: A002
        self.name = name
        self.type = type
        self.input = ""
        self.output = ""
        self.is_error = False
        self.updated = False
        FakeStep.created.append(self)

    async def send(self) -> None:
        pass

    async def update(self) -> None:
        self.updated = True


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    """Ход рисуется на подменённых Message/Step: chainlit и postgres не нужны.

    Подменяет autouse-фикстуру из conftest, которая поднимает их для тестов
    data layer.
    """


@pytest.fixture(autouse=True)
def chainlit_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeMessage.sent = []
    FakeStep.created = []
    monkeypatch.setattr(turn_module.cl, "Message", FakeMessage)
    monkeypatch.setattr(turn_module.cl, "Step", FakeStep)


@pytest.fixture
def config() -> HermesConfig:
    return HermesConfig(base_url="http://hermes:8642/", api_key="k" * 32)


def client_for(config: HermesConfig, body: str, requests: list | None = None):
    """Клиент поверх мока: сессия заводится, ход отдаётся готовым потоком."""

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if request.url.path.endswith("/chat/stream"):
            return httpx.Response(200, content=body.encode())

        return httpx.Response(
            201,
            json={
                "session": {
                    "id": SESSION,
                    "source": "api_server",
                    "started_at": 1785168444.0,
                }
            },
        )

    return HermesApiClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        config,
        profile=PROFILE,
    )


async def test_turn_creates_session_before_stream(config: HermesConfig):
    seen: list[httpx.Request] = []

    await HermesTurn(client_for(config, TURN, seen)).run(SESSION, "покажи")

    # chat/stream работает только с заведённой сессией
    assert [r.url.path.split("/api/sessions")[1] for r in seen] == [
        "",
        f"/{SESSION}/chat/stream",
    ]


async def test_turn_streams_answer(config: HermesConfig):
    await HermesTurn(client_for(config, TURN)).run(SESSION, "покажи")

    answer = FakeMessage.sent[0]
    assert answer.content == "boba-hermes"
    # ответ собирается из дельт, а не ставится целиком в конце
    assert answer.streamed == 2


async def test_turn_shows_tool_step(config: HermesConfig):
    await HermesTurn(client_for(config, TURN)).run(SESSION, "покажи")

    step = FakeStep.created[0]
    assert (step.name, step.type) == ("terminal", "tool")
    assert step.input == "echo boba-hermes"
    assert step.updated is True
    assert step.is_error is False


async def test_failed_tool_marks_step(config: HermesConfig):
    body = (
        'event: tool.started\ndata: {"message_id": "m", "tool_name": "terminal",'
        ' "preview": "rm -rf /"}\n\n'
        'event: tool.failed\ndata: {"message_id": "m", "tool_name": "terminal"}\n\n'
    )

    await HermesTurn(client_for(config, body)).run(SESSION, "удали")

    assert FakeStep.created[0].is_error is True


async def test_repeated_tool_closes_steps_in_order(config: HermesConfig):
    body = (
        'event: tool.started\ndata: {"tool_name": "terminal", "preview": "первый"}\n\n'
        'event: tool.started\ndata: {"tool_name": "terminal", "preview": "второй"}\n\n'
        'event: tool.completed\ndata: {"tool_name": "terminal"}\n\n'
    )

    await HermesTurn(client_for(config, body)).run(SESSION, "давай")

    # закрываем в порядке открытия: другой связки событий api_server не даёт
    assert [step.input for step in FakeStep.created] == ["первый", "второй"]
    assert [step.updated for step in FakeStep.created] == [True, False]


async def test_answer_without_deltas_comes_from_completed(config: HermesConfig):
    body = (
        'event: assistant.completed\ndata: {"message_id": "m",'
        ' "content": "готово", "completed": true}\n\n'
    )

    await HermesTurn(client_for(config, body)).run(SESSION, "сделай")

    assert FakeMessage.sent[0].content == "готово"


async def test_error_event_becomes_agent_error(config: HermesConfig):
    body = 'event: error\ndata: {"message": "provider auth failed"}\n\n'

    with pytest.raises(AgentError) as failure:
        await HermesTurn(client_for(config, body)).run(SESSION, "привет")

    # текст hermes нужен в логе, пользователю показывается код ошибки
    assert "provider auth failed" in failure.value.internal_detail
