"""Книга ответов стенда модели: ответ по обычному тексту вопроса, ход по
сыгранным вызовам, пауза потока до release, отказ на неизвестный вопрос и
прежние маркеры рядом с книгой.

Стенд идёт отдельным процессом, как у ui-тестов чата; запросы — те, что шлёт
агент по протоколу openai: сообщение пользователя со служебным блоком
`<system-reminder>` перед набранным текстом, поток — SSE-кадрами.
"""

from __future__ import annotations

import json
import multiprocessing
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, ClassVar

import httpx
import pytest

from boba.agent.tags import AgentTag
from boba.stand.ui.fake_llm import (
    FakeRoute,
    Reply,
    ReplyBookClient,
    ReplyCall,
    ReplyTurn,
    ScenarioError,
    ScenarioName,
    serve,
)
from boba.stand.ui.stand import StandPorts, StandUrl

STATUS_QUESTION = "Какой сегодня статус выгрузки?"
STATUS_ANSWER = "Выгрузка закончилась в 06:10, ошибок нет."
DIAGRAM_QUESTION = "Нарисуй схему: пользователь оформляет заказы"
DIAGRAM_INTENT = "Сохраняю диаграмму заказов"
DIAGRAM_ANSWER = (
    "Диаграмма **orders.mmd**: пользователь оформляет ноль или больше заказов."
)
STEPS_QUESTION = "Опиши шаги миграции"
STEPS_ANSWER = "снять копию перенести схему перелить данные проверить счётчики"
OFFERED_DIAGRAM = "mcp__boba__diagram_save"
HOLD_AFTER = 3
HELD_TIMEOUT_SEC = 10.0


class LlmWire:
    """Запросы агента к стенду по протоколу openai: тело, разбор ответа и SSE."""

    DATA: ClassVar[str] = "data: "
    DONE: ClassVar[str] = "[DONE]"

    def __init__(self, base_url: str) -> None:
        self._url = f"{base_url}{FakeRoute.COMPLETIONS.value}"

    def body(
        self, question: str, history: Sequence[Mapping[str, Any]], stream: bool
    ) -> dict[str, Any]:
        reminder = AgentTag.SYSTEM_REMINDER.wrap_block("today is 2026-10-17")
        messages: list[Mapping[str, Any]] = [
            {"role": "system", "content": "you are the stand agent"},
            {"role": "user", "content": f"{reminder}\n{question}"},
            *history,
        ]
        tool = {
            "type": "function",
            "function": {
                "name": OFFERED_DIAGRAM,
                "description": "saves a diagram",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        return {
            "model": "fake-model",
            "messages": messages,
            "tools": [tool],
            "stream": stream,
        }

    def ask(
        self, question: str, history: Sequence[Mapping[str, Any]] = ()
    ) -> httpx.Response:
        body = self.body(question, history, stream=False)
        return httpx.post(self._url, json=body, timeout=30.0)

    @staticmethod
    def message(response: httpx.Response) -> Mapping[str, Any]:
        assert response.status_code == httpx.codes.OK, response.text
        return response.json()["choices"][0]["message"]

    def stream(self, question: str, sink: list[Mapping[str, Any]]) -> None:
        """Читает поток кадрами в sink, пока стенд не закончит ответ."""
        body = self.body(question, (), stream=True)
        with httpx.stream("POST", self._url, json=body, timeout=60.0) as response:
            for line in response.iter_lines():
                if not line.startswith(self.DATA):
                    continue

                data = line.removeprefix(self.DATA)
                if data == self.DONE:
                    return

                sink.append(json.loads(data))


class Frames:
    """Кадры потока, прочитанные другим потоком исполнения."""

    def __init__(self, frames: Sequence[Mapping[str, Any]]) -> None:
        self._frames = frames

    def contents(self) -> list[str]:
        found: list[str] = []
        for frame in list(self._frames):
            delta = frame["choices"][0]["delta"]
            if content := delta.get("content"):
                found.append(content)

        return found


@pytest.fixture(scope="module")
def stand_url() -> Iterator[str]:
    port = StandPorts().free()
    process = multiprocessing.Process(
        target=serve, args=(StandUrl.HOST.value, port, 0.0), daemon=True
    )
    process.start()
    base_url = StandUrl.of(port)
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                httpx.get(f"{base_url}{FakeRoute.HEALTH.value}", timeout=1.0)
                break
            except httpx.TransportError:
                time.sleep(0.1)

        yield base_url
    finally:
        process.terminate()
        process.join(timeout=10)


@pytest.fixture
def book(stand_url: str) -> ReplyBookClient:
    client = ReplyBookClient(stand_url)
    diagram = ReplyCall(
        tool="diagram_save",
        arguments={
            "name": "orders.mmd",
            "spec": "erDiagram\n    USER ||--o{ ORDER : places",
        },
        intent=DIAGRAM_INTENT,
    )
    client.register(
        [
            Reply(question=STATUS_QUESTION, turns=[ReplyTurn(content=STATUS_ANSWER)]),
            Reply(
                question=DIAGRAM_QUESTION,
                turns=[
                    ReplyTurn(
                        reasoning="Нужна ER-диаграмма двух сущностей",
                        calls=[diagram],
                    ),
                    ReplyTurn(content=DIAGRAM_ANSWER),
                ],
            ),
            Reply(
                question=STEPS_QUESTION,
                turns=[
                    ReplyTurn(
                        content=STEPS_ANSWER, hold=True, hold_after_tokens=HOLD_AFTER
                    )
                ],
                token_delay_sec=0.01,
            ),
        ]
    )
    return client


def test_plain_question_is_answered_from_the_book(
    book: ReplyBookClient, stand_url: str
) -> None:
    message = LlmWire.message(LlmWire(stand_url).ask(STATUS_QUESTION))

    assert message["content"] == STATUS_ANSWER


def test_turn_follows_the_calls_played_after_the_question(
    book: ReplyBookClient, stand_url: str
) -> None:
    wire = LlmWire(stand_url)

    first = wire.message(wire.ask(DIAGRAM_QUESTION))
    call = first["tool_calls"][0]
    arguments = json.loads(call["function"]["arguments"])
    assert call["function"]["name"] == OFFERED_DIAGRAM
    assert arguments["intent"] == DIAGRAM_INTENT
    assert arguments["name"] == "orders.mmd"

    history = [
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call["id"], "content": "diagram saved"},
    ]
    second = wire.message(wire.ask(DIAGRAM_QUESTION, history))
    assert second["content"] == DIAGRAM_ANSWER

    repeated = wire.message(wire.ask(DIAGRAM_QUESTION))
    assert repeated["tool_calls"][0]["id"] == call["id"]


def test_held_stream_stops_after_the_given_tokens_until_release(
    book: ReplyBookClient, stand_url: str
) -> None:
    frames: list[Mapping[str, Any]] = []
    reader = threading.Thread(
        target=LlmWire(stand_url).stream, args=(STEPS_QUESTION, frames), daemon=True
    )
    reader.start()

    book.await_held(STEPS_QUESTION, HELD_TIMEOUT_SEC)
    seen = Frames(frames)
    deadline = time.monotonic() + HELD_TIMEOUT_SEC
    while len(seen.contents()) < HOLD_AFTER and time.monotonic() < deadline:
        time.sleep(0.05)

    time.sleep(0.3)
    assert "".join(seen.contents()) == " ".join(STEPS_ANSWER.split(" ")[:HOLD_AFTER])
    assert reader.is_alive()

    book.release(STEPS_QUESTION)
    reader.join(timeout=HELD_TIMEOUT_SEC)

    assert not reader.is_alive()
    assert "".join(seen.contents()) == STEPS_ANSWER
    assert STEPS_QUESTION not in book.held()


def test_unknown_question_is_refused_with_the_nearest(
    book: ReplyBookClient, stand_url: str
) -> None:
    response = LlmWire(stand_url).ask("Какой сегодня статус?")

    assert response.status_code == httpx.codes.BAD_REQUEST
    message = response.json()["error"]["message"]
    assert "no reply for question 'Какой сегодня статус?'" in message
    assert STATUS_QUESTION in message


def test_markers_still_work_next_to_the_book(
    book: ReplyBookClient, stand_url: str
) -> None:
    message = LlmWire.message(LlmWire(stand_url).ask(ScenarioName.ANSWER.value))

    assert message["content"].startswith("Here is a plain streamed answer")


def test_one_question_with_two_replies_is_refused(stand_url: str) -> None:
    client = ReplyBookClient(stand_url)
    first = Reply(question=STATUS_QUESTION, turns=[ReplyTurn(content="один")])
    second = Reply(question=f"  {STATUS_QUESTION} ", turns=[ReplyTurn(content="два")])

    with pytest.raises(ScenarioError, match="409"):
        client.register([first, second])

    client.register([first, first])


def test_release_of_an_unknown_question_is_refused(
    book: ReplyBookClient,
) -> None:
    with pytest.raises(ScenarioError, match="404"):
        book.release("Такого вопроса нет")
