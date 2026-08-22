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
from ui.stand import StandConfig, StandProcess, free_port

pytestmark = pytest.mark.ui

BOOT_TIMEOUT_SEC = 180.0
"""Подъём стенда с песочницей: восемь зигот, у kb — прогрев эмбеддера."""

TURN_TIMEOUT_SEC = 180.0


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

    def message(self) -> str:
        """Сообщение пользователю: фейковый провайдер сделает из него tool_call."""
        request = {"name": self.tool, "arguments": self.arguments}

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
        arguments={"url": "http://127.0.0.1:1/"},
        expect="web_fetch_page",
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
        self, sandbox_chat: ChatPage, case: ToolCase
    ) -> None:
        sandbox_chat.ask(case.message())
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
