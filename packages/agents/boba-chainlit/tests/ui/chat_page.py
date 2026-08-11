"""Вкладка чата глазами теста: отправка вопроса и чтение шагов ленты.

Ошибки: ChatPageError — ожидаемый элемент не появился за отведённое время.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ui.socket_log import ChatEvent, SocketLog

__all__ = ["ChatPage", "ChatPageError", "Selector", "StepKind", "TextSample"]


class ChatPageError(RuntimeError):
    """Элемент ленты не дождались."""


class Selector(StrEnum):
    """Селекторы фронта chainlit, на которые опирается тест."""

    INPUT = "#chat-input"
    SUBMIT = "#chat-submit"
    STEP = "[data-step-type]"

    @staticmethod
    def of_type(step_type: str) -> str:
        return f'[data-step-type="{step_type}"]'


class StepKind(StrEnum):
    """Типы шагов ленты в разметке chainlit."""

    USER = "user_message"
    ASSISTANT = "assistant_message"
    RUN = "run"
    TOOL = "tool"
    LLM = "llm"


@dataclass(frozen=True)
class TextSample:
    """Снимок текста узла в момент времени."""

    at_sec: float
    text: str

    @property
    def size(self) -> int:
        return len(self.text)


@dataclass
class ChatPage:
    """Страница чата: ввод вопроса и наблюдение за шагами."""

    page: Page
    log: SocketLog
    base_url: str
    default_timeout_ms: float = 60_000

    ON_CHAT_START: ClassVar[str] = "on_chat_start"

    def open(self) -> None:
        self.page.goto(f"{self.base_url}/", wait_until="domcontentloaded")
        self._await(Selector.INPUT.value)
        self._await_chat_start()

    def _await_chat_start(self, timeout_sec: float = 30.0) -> None:
        """Свой ход chainlit рисует на старте сессии: тест ждёт его окончания.

        Иначе ожидание конца хода срабатывает на чужой паре task_start/task_end.
        """
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if self.log.has_step_named(self.ON_CHAT_START):
                return

            self.page.wait_for_timeout(100)

        raise ChatPageError(f"chat start is not drawn\n{self.log.describe()}")

    def ask(self, text: str) -> None:
        """Задаёт вопрос; журнал очищается, чтобы в нём был только этот ход."""
        self.log.clear()
        field = self.page.locator(Selector.INPUT.value)
        field.click()
        field.fill(text)
        self.page.locator(Selector.SUBMIT.value).click()

    def steps_of(self, step_type: str) -> Locator:
        return self.page.locator(Selector.of_type(step_type))

    def expand_process(self) -> None:
        """Раскрывает контейнер процесса: вложенные шаги свёрнуты по умолчанию."""
        self._expand(StepKind.RUN.value)

    def expand_step(self, step_type: str) -> Locator:
        """Раскрывает вложенный шаг и отдаёт его локатор: вывод тоже свёрнут."""
        self._expand(step_type)
        return self.page.locator(Selector.of_type(step_type)).first

    def _expand(self, step_type: str) -> None:
        node = self.page.locator(Selector.of_type(step_type))
        if not node.count():
            raise ChatPageError(f"step {step_type} is not drawn\n{self.dom()[:2000]}")

        node.first.click()
        self.page.wait_for_timeout(300)

    def await_step(self, step_type: str, timeout_ms: float | None = None) -> Locator:
        """Ждёт первый шаг указанного типа и отдаёт его локатор."""
        selector = Selector.of_type(step_type)
        self._await(selector, timeout_ms)
        return self.page.locator(selector).first

    def await_tokens(self, step_id: str, count: int, timeout_sec: float = 30.0) -> None:
        """Ждёт, пока в шаг прилетит нужное число токенов."""
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if len(self.log.tokens_of(step_id)) >= count:
                return

            self.page.wait_for_timeout(50)

        got = len(self.log.tokens_of(step_id))
        raise ChatPageError(
            f"step {step_id} got {got} tokens, expected {count}\n{self.log.describe()}"
        )

    def await_idle(self, timeout_sec: float = 60.0) -> None:
        """Ждёт конца хода: task_end, пришедший после task_start этого хода.

        Одного task_end мало: chainlit гасит индикатор и на старте сессии, и
        ожидание завершилось бы до первого шага.
        """
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if self._turn_finished():
                return

            self.page.wait_for_timeout(100)

        raise ChatPageError(f"turn is not finished\n{self.log.describe()}")

    def _turn_finished(self) -> bool:
        started = -1
        for index, frame in enumerate(self.log.frames):
            if frame.event is ChatEvent.TASK_START:
                started = index
                continue

            if frame.event is not ChatEvent.TASK_END:
                continue

            if started >= 0:
                return True

        return False

    def sample_text(
        self,
        locator: Locator,
        samples: int,
        interval_ms: float,
    ) -> Sequence[TextSample]:
        """Серия снимков текста узла: по ней видно, рос он или возник целиком."""
        started = time.monotonic()
        taken: list[TextSample] = []
        for _ in range(samples):
            text = ""
            if locator.count():
                text = locator.first.inner_text()

            taken.append(TextSample(at_sec=time.monotonic() - started, text=text))
            self.page.wait_for_timeout(interval_ms)

        return taken

    def dom(self) -> str:
        """Разметка ленты: её печатает упавший тест."""
        return self.page.locator("#root").inner_html()

    def _await(self, selector: str, timeout_ms: float | None = None) -> None:
        wait = timeout_ms
        if wait is None:
            wait = self.default_timeout_ms

        try:
            self.page.wait_for_selector(selector, timeout=wait, state="attached")
        except PlaywrightTimeout as exc:
            raise ChatPageError(f"selector is not found: {selector}") from exc
