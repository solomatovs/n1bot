"""Лента в браузере: живой ход и тред после перезагрузки рисуются одинаково
(те же шаги, те же id, тот же текст), а вторая вкладка того же треда видит
токены чужого хода.
"""

from __future__ import annotations

from typing import Any

import pytest
from chat_ui import ChatOpener
from playwright.sync_api import Locator

from boba.stand.ui.chat_page import ChatPage, Selector, StepKind
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.stand import StandProcess

pytestmark = pytest.mark.ui

PULSE = "\u200b"
COMPARED = (StepKind.USER.value, StepKind.LLM.value, StepKind.ASSISTANT.value)
OWN_STEPS = ("on_chat_start", "on_chat_resume")
"""Шаги самого chainlit вокруг обработчиков: в ленте треда их нет."""


def _steps(chat: ChatPage) -> dict[str, dict[str, Any]]:
    """Последняя нагрузка каждого шага ленты по id, кроме кружка ожидания."""
    found: dict[str, dict[str, Any]] = {}
    for kind in (StepKind.USER, StepKind.RUN, StepKind.LLM, StepKind.ASSISTANT):
        for payload in chat.log.steps_of_type(kind.value):
            if payload.get("output") == PULSE:
                continue

            if payload.get("name") in OWN_STEPS:
                continue

            found[str(payload.get("id"))] = payload

    return found


def _outline(chat: ChatPage) -> list[str]:
    """Типы шагов в порядке DOM; process раскрыт, чтобы дети были в дереве."""
    chat.expand_process()
    outline: list[str] = []
    for node in chat.page.locator(Selector.STEP.value).all():
        outline.append(str(node.get_attribute("data-step-type")))

    return outline


def _texts(chat: ChatPage) -> dict[str, list[str]]:
    """Тексты шагов сравниваемых типов из DOM по типу, в порядке DOM."""
    chat.expand_process()
    texts: dict[str, list[str]] = {}
    for kind in COMPARED:
        node: Locator
        for node in chat.page.locator(Selector.of_type(kind)).all():
            texts.setdefault(kind, []).append(node.inner_text().strip())

    return texts


def _thread_url(chat: ChatPage) -> str:
    """Адрес треда вкладки: открытие по нему идёт возвратом к треду (resume)."""
    return f"{chat.base_url}/thread/{chat.log.thread_id()}"


def _reload(chat: ChatPage) -> None:
    """Открытие страницы треда заново: лента приходит воспроизведением истории."""
    url = _thread_url(chat)
    chat.log.clear()
    chat.page.goto(url, wait_until="domcontentloaded")
    chat.page.wait_for_selector(Selector.INPUT.value, timeout=60000)
    chat.await_step(StepKind.ASSISTANT.value)
    chat.page.wait_for_timeout(1500)


class TestReplayLooksLikeLive:
    """Перезагруженный тред — та же лента, что была живой."""

    def test_steps_ids_and_texts_match_after_reload(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.THINKING_ANSWER.value} please")
        chat.await_idle()
        live_steps = _steps(chat)
        live_outline = _outline(chat)
        live_texts = _texts(chat)
        if not live_steps:
            raise AssertionError(chat.log.describe())

        _reload(chat)

        replayed_steps = _steps(chat)
        for kind in (*COMPARED, StepKind.RUN.value):
            live_ids = {i for i, s in live_steps.items() if s.get("type") == kind}
            again = {i for i, s in replayed_steps.items() if s.get("type") == kind}
            if live_ids != again:
                raise AssertionError(
                    f"{kind}: live ids {sorted(live_ids)} != replayed {sorted(again)}"
                    f"\n{chat.log.describe()}"
                )

        for step_id in live_steps:
            kind = live_steps[step_id].get("type")
            if kind not in COMPARED:
                continue

            again = replayed_steps[step_id]
            if live_steps[step_id].get("output") != again.get("output"):
                raise AssertionError(
                    f"{kind} {step_id}: output differs after reload: "
                    f"{live_steps[step_id].get('output')!r} != "
                    f"{replayed_steps[step_id].get('output')!r}"
                )

        if _outline(chat) != live_outline:
            raise AssertionError(
                f"DOM outline after reload {_outline(chat)} != live {live_outline}"
            )

        if _texts(chat) != live_texts:
            raise AssertionError(f"DOM texts differ: {_texts(chat)} != {live_texts}")

    def test_tool_step_survives_reload(self, chat: ChatPage) -> None:
        chat.ask(f"{ScenarioName.DIAGRAM.value} please")
        chat.await_idle()
        live_tools = chat.log.steps_of_type(StepKind.TOOL.value)
        if not live_tools:
            raise AssertionError(chat.log.describe())

        live_ids = {str(step.get("id")) for step in live_tools}
        live_names = {str(step.get("name")) for step in live_tools}

        _reload(chat)

        again = chat.log.steps_of_type(StepKind.TOOL.value)
        if {str(step.get("id")) for step in again} != live_ids:
            raise AssertionError(f"tool ids differ: {again}\n{chat.log.describe()}")

        final_names = {str(step.get("name")) for step in again}
        if not final_names & live_names:
            raise AssertionError(f"tool names differ: {final_names} vs {live_names}")

        chat.expand_process()
        step = chat.expand_step(StepKind.TOOL.value)
        if "diagram_save" not in step.inner_text():
            raise AssertionError(step.inner_text())


class TestSecondTabFollows:
    """Вторая вкладка треда получает токены хода, идущего в первой."""

    def test_other_tab_streams_the_live_turn(
        self, chat: ChatPage, module_chats: ChatOpener, stand: StandProcess
    ) -> None:
        chat.ask(f"{ScenarioName.ANSWER.value} opener")
        chat.await_idle()
        thread_url = _thread_url(chat)

        other = module_chats.open(stand)
        other.page.goto(thread_url, wait_until="domcontentloaded")
        other.page.wait_for_selector(Selector.INPUT.value, timeout=60000)
        other.await_step(StepKind.ASSISTANT.value)
        other.log.clear()

        chat.ask(f"{ScenarioName.LONG.value} follow me")
        chat.await_idle()

        streamed = other.log.streamed_steps(StepKind.ASSISTANT.value)
        for _ in range(40):
            if streamed:
                break

            other.page.wait_for_timeout(500)
            streamed = other.log.streamed_steps(StepKind.ASSISTANT.value)

        if not streamed:
            raise AssertionError(
                f"the other tab saw no streamed answer\n{other.log.describe()}"
            )

        tokens = other.log.tokens_of(streamed[-1])
        if len(tokens) < 2:
            raise AssertionError(f"too few tokens in the other tab: {tokens}")

        live_answers = {
            str(s.get("id")) for s in chat.log.steps_of_type(StepKind.ASSISTANT.value)
        }
        if streamed[-1] not in live_answers:
            raise AssertionError(
                f"the other tab streamed step {streamed[-1]} unknown to the holder"
            )
