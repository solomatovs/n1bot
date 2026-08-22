"""Каждый инструмент вызывается ходом и рисуется в ленте: проверка по DOM.

Стенд поднимается с включённой песочницей, то есть тем же путём, что и прод:
зигота секции, исполнитель вызова, тело инструмента. Модель просят вызвать
конкретный инструмент, после чего в разметке ленты ищется его шаг: он обязан
быть закрыт галочкой, а не крестом, и содержать ожидаемый след работы.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ui.chat_page import ChatPage, StepKind
from ui.fake_llm import ScenarioName
from ui.conftest import StandDatabase
from ui.stand import StandConfig, StandProcess, free_port

pytestmark = pytest.mark.ui

BOOT_TIMEOUT_SEC = 180.0
"""Подъём стенда с песочницей: восемь зигот, у kb — прогрев эмбеддера."""

TURN_TIMEOUT_SEC = 180.0

STREAM_ELEMENT = "CanvasStream"
"""Имя элемента кнопки живого вывода на шаге инструмента песочницы."""


class StepMark:
    """Статусный кружок в названии шага: им лента показывает исход вызова."""

    DONE: ClassVar[str] = "✔"
    FAILED: ClassVar[str] = "✖"


@dataclass(frozen=True)
class ToolCase:
    """Инструмент, аргументы вызова и след, по которому видно, что он отработал."""

    tool: str
    arguments: dict[str, Any]
    expect: str
    """Фрагмент, который обязан быть в разметке шага после успешного вызова."""

    LLM_PORT: ClassVar[str] = "{llm_port}"
    """Плейсхолдер порта фейкового сервера в строковых аргументах."""

    def message(self, llm_port: int) -> str:
        """Сообщение пользователю: фейковый провайдер сделает из него tool_call."""
        arguments: dict[str, Any] = {}
        for name, value in self.arguments.items():
            if isinstance(value, str):
                value = value.replace(self.LLM_PORT, str(llm_port))
            arguments[name] = value

        request = {"name": self.tool, "arguments": arguments}

        return f"{ScenarioName.CALL.value} {json.dumps(request, ensure_ascii=False)}"


CASES: tuple[ToolCase, ...] = (
    ToolCase(
        tool="bash",
        arguments={"command": "echo ui-probe-bash"},
        expect="ui-probe-bash",
    ),
    ToolCase(
        tool="pg_list_targets",
        arguments={},
        expect="main",
    ),
    ToolCase(
        tool="ch_list_targets",
        arguments={},
        expect="main",
    ),
    ToolCase(
        tool="kb_fts_search",
        arguments={"query": "paas", "top_k": 3},
        expect="kb_fts_search",
    ),
    ToolCase(
        tool="kb_vector_search",
        arguments={"query": "paas", "top_k": 3},
        expect="kb_vector_search",
    ),
    ToolCase(
        tool="web_fetch_page",
        arguments={
            "url": "http://127.0.0.1:{llm_port}/page",
            "as_markdown": True,
            "line_offset": 0,
            "line_count": 50,
        },
        expect="stand page",
    ),
)
"""По случаю на инструмент: имя, аргументы и след успешной работы в ленте."""


@pytest.fixture(scope="module")
def sandbox_stand(
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
) -> Iterator[StandProcess]:
    """Стенд с песочницей: инструменты идут через зиготы, как в проде."""
    config = StandConfig(
        workdir=stand_workdir / "sandbox",
        app_port=free_port(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-sandbox",
        sandbox=True,
    )
    process = StandProcess(config=config, log_path=stand_workdir / "sandbox-app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


@pytest.fixture
def sandbox_chat(sandbox_stand: StandProcess, open_chat: Any) -> ChatPage:
    """Вкладка чата на стенде с песочницей: фабрика та же, что у прочих тестов."""
    return open_chat(sandbox_stand)


class TestToolsInFeed:
    """Инструмент вызван моделью и закрыт в ленте галочкой, а не крестом."""

    @pytest.mark.parametrize("case", CASES, ids=lambda case: case.tool)
    def test_tool_call_is_drawn_and_finished(
        self,
        sandbox_chat: ChatPage,
        sandbox_stand: StandProcess,
        llm_port: int,
        case: ToolCase,
    ) -> None:
        sandbox_chat.ask(case.message(llm_port))
        sandbox_chat.await_idle(timeout_sec=TURN_TIMEOUT_SEC)

        sandbox_chat.expand_process()
        step = sandbox_chat.expand_step(StepKind.TOOL.value)
        markup = step.inner_text()

        if case.tool not in markup:
            raise AssertionError(
                f"шага инструмента {case.tool} нет в ленте\n{sandbox_chat.dom()[:4000]}"
            )

        if StepMark.FAILED in markup:
            raise AssertionError(
                f"инструмент {case.tool} завершился ошибкой:\n{markup}"
            )

        if StepMark.DONE not in markup:
            raise AssertionError(f"инструмент {case.tool} не закрыт:\n{markup}")

        if case.expect not in markup:
            raise AssertionError(
                f"в шаге {case.tool} нет следа работы {case.expect!r}:\n{markup}"
            )

        complaints = sandbox_stand.complaints()
        if complaints:
            raise AssertionError(
                f"ход {case.tool} оставил ошибки в логе стенда:\n"
                + "\n".join(complaints[:10])
            )

    def test_stream_button_reaches_the_data_layer(
        self,
        sandbox_chat: ChatPage,
        sandbox_stand: StandProcess,
        stand_db: StandDatabase,
        llm_port: int,
    ) -> None:
        """Элемент кнопки потока bash-шага записан в базу: колбэки трасера
        идут в loop приложения, а не в чужой — иначе запись молча терялась."""
        before = stand_db.elements_named(STREAM_ELEMENT)

        sandbox_chat.ask(CASES[0].message(llm_port))
        sandbox_chat.await_idle(timeout_sec=TURN_TIMEOUT_SEC)

        after = stand_db.elements_named(STREAM_ELEMENT)
        if after <= before:
            raise AssertionError(
                f"элемент {STREAM_ELEMENT} не записан: было {before}, стало {after}\n"
                + sandbox_stand.tail(60)
            )
