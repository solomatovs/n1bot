"""Вкладка чата глазами теста: отправка вопроса и чтение шагов ленты.

Ошибки: ChatPageError — ожидаемый элемент не появился за отведённое время.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from boba.stand.ui.socket_log import ChatEvent, SocketLog

__all__ = ["ChatPage", "ChatPageError", "Selector", "StepKind"]


class ChatPageError(RuntimeError):
    """Элемент ленты не дождались."""


class Selector(StrEnum):
    """Селекторы фронта chainlit, на которые опирается тест."""

    INPUT = "#chat-input"
    SUBMIT = "#chat-submit"
    STEP = "[data-step-type]"
    STEP_TOGGLE = 'button[id^="step-"]'
    TOGGLE_STATE = "data-state"
    OPENED = "open"
    PROFILES = "#chat-profiles"
    PROFILE_ITEM = '[data-test^="select-item:"]'
    AVATAR = 'img[alt^="Avatar for"]'

    @staticmethod
    def of_type(step_type: str) -> str:
        return f'[data-step-type="{step_type}"]'

    @staticmethod
    def of_profile(name: str) -> str:
        return f'[data-test="select-item:{name}"]'


class StepKind(StrEnum):
    """Типы шагов ленты в разметке chainlit."""

    USER = "user_message"
    ASSISTANT = "assistant_message"
    RUN = "run"
    TOOL = "tool"
    LLM = "llm"


@dataclass
class ChatPage:
    """Страница чата: ввод вопроса и наблюдение за шагами."""

    page: Page
    log: SocketLog
    base_url: str
    default_timeout_ms: float = 60_000

    ON_CHAT_START: ClassVar[str] = "on_chat_start"

    MENU_ESCAPE_X: ClassVar[int] = 20
    """X координатного клика мимо меню профилей: левый край вкладки."""

    MENU_ESCAPE_Y: ClassVar[int] = 500
    """Y того же клика: ниже выпадающего списка, но выше поля ввода."""

    def open(self) -> None:
        self.page.goto(f"{self.base_url}/", wait_until="domcontentloaded")
        self._await(Selector.INPUT.value)
        self._await_chat_start()

    def open_thread(
        self, thread_id: str, last_step_id: str, timeout_sec: float = 30.0
    ) -> None:
        """Вкладка переходит в тред и ждёт, пока воспроизведение истории дойдёт
        до шага last_step_id: всё, что придёт после, — новое, а не повтор."""
        self.log.clear()
        self.page.goto(
            f"{self.base_url}/thread/{thread_id}", wait_until="domcontentloaded"
        )
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if self.log.index_of(ChatEvent.NEW_MESSAGE, last_step_id) >= 0:
                return

            self.page.wait_for_timeout(100)

        msg = (
            f"chat page: thread {thread_id} did not replay step {last_step_id} "
            f"within {timeout_sec}s\n{self.log.describe()}"
        )
        raise ChatPageError(msg)

    def _await_chat_start(self, timeout_sec: float = 30.0) -> None:
        """Свой ход chainlit рисует на старте сессии: тест ждёт его окончания.

        Иначе ожидание конца хода срабатывает на чужой паре task_start/task_end:
        вопрос, заданный посреди on_chat_start, открывает свою задачу, а
        закрывает её task_end обработчика старта.
        """
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if self.log.task_done(self.ON_CHAT_START):
                return

            self.page.wait_for_timeout(100)

        msg = (
            f"chat page: the {self.ON_CHAT_START!r} task did not end within "
            f"{timeout_sec}s\n{self.log.describe()}"
        )
        raise ChatPageError(msg)

    def has_profile_selector(self) -> bool:
        """Есть ли на странице селектор профилей чата."""
        return bool(self.page.locator(Selector.PROFILES.value).count())

    def profile_label(self) -> str:
        """Текст селектора профилей: какой профиль выбран сейчас."""
        selector = self._profile_selector()

        return selector.first.inner_text().strip()

    def open_profile_menu(self) -> list[str]:
        """Открывает меню профилей и отдаёт имена пунктов в порядке DOM."""
        selector = self._profile_selector()
        selector.first.click()
        self._await(Selector.PROFILE_ITEM.value)

        names: list[str] = []
        items = self.page.locator(Selector.PROFILE_ITEM.value)
        for index in range(items.count()):
            item = items.nth(index)
            if not item.is_visible():
                continue

            value = item.get_attribute("data-test")
            if value is None:
                continue

            names.append(value.removeprefix("select-item:"))

        return names

    def profile_menu_items(self) -> int:
        """Сколько пунктов меню профилей сейчас в разметке."""
        return self.page.locator(Selector.PROFILE_ITEM.value).count()

    def profile_menu_open(self) -> bool:
        """Открыто ли меню: состояние держит сам триггер (aria-expanded)."""
        selector = self._profile_selector()

        return selector.first.get_attribute("aria-expanded") == "true"

    def close_profile_menu(self) -> None:
        """Закрывает меню профилей, ничего не выбирая: клик мимо него.

        Меню держит оверлей, перехватывающий события, поэтому закрывает не
        клик по элементу страницы, а координатный клик в стороне.
        """
        self.page.mouse.click(self.MENU_ESCAPE_X, self.MENU_ESCAPE_Y)
        self.page.wait_for_timeout(300)

    def select_profile(self, name: str) -> None:
        """Выбирает профиль из открытого меню и ждёт перезапуска чата."""
        item = self.page.locator(Selector.of_profile(name))
        if not item.count():
            msg = self._missing(
                f"profile menu item {name!r}", Selector.of_profile(name)
            )
            raise ChatPageError(msg)

        self.log.clear()
        item.first.click()
        self._await_chat_start()

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
        """Раскрывает контейнер процесса: вложенные шаги свёрнуты по умолчанию.

        Контейнер без вложенных шагов фронт рисует строкой, которую нечем
        раскрыть: клик по ней пропал бы, а шаги пришли бы уже в свёрнутый
        контейнер. Поэтому клик ждёт, пока у контейнера появится раскрытие.
        """
        container = self.page.locator(Selector.of_type(StepKind.RUN.value)).first
        container.locator(Selector.STEP_TOGGLE.value).first.wait_for(
            state="attached", timeout=self.default_timeout_ms
        )
        self._open(container)

    def expand_step(self, step_type: str) -> Locator:
        """Раскрывает вложенный шаг и отдаёт его локатор: вывод тоже свёрнут."""
        self._expand(step_type)
        return self.page.locator(Selector.of_type(step_type)).first

    def expand_last_tool(self) -> Locator:
        """Раскрывает последний ход и его последний шаг инструмента.

        В одном чате ходов несколько, а раскрытие первого шага показало бы
        давно прошедший вызов.
        """
        self._expand_last(StepKind.RUN.value)
        self._expand_last(StepKind.TOOL.value)
        return self.page.locator(Selector.of_type(StepKind.TOOL.value)).last

    def expand_last_run(self) -> None:
        """Раскрывает последний ход: его шаги инструментов свёрнуты внутри."""
        self._expand_last(StepKind.RUN.value)

    def expand_tool_titled(self, title: str) -> Locator:
        """Раскрывает последний шаг инструмента, в чьём заголовке есть title:
        в ходе с несколькими вызовами шаг ищется по подписи вызова."""
        node = self.page.locator(Selector.of_type(StepKind.TOOL.value)).filter(
            has_text=title
        )
        if not node.count():
            selector = Selector.of_type(StepKind.TOOL.value)
            msg = self._missing(f"tool step titled {title!r}", selector)
            raise ChatPageError(msg)

        node.last.click()
        self.page.wait_for_timeout(300)
        return node.last

    def _expand(self, step_type: str) -> None:
        node = self.page.locator(Selector.of_type(step_type))
        if not node.count():
            msg = self._missing(f"step {step_type}", Selector.of_type(step_type))
            raise ChatPageError(msg)

        self._open(node.first)

    def _expand_last(self, step_type: str) -> None:
        node = self.page.locator(Selector.of_type(step_type))
        if not node.count():
            msg = self._missing(f"step {step_type}", Selector.of_type(step_type))
            raise ChatPageError(msg)

        self._open(node.last)

    def _open(self, step: Locator) -> None:
        """Раскрывает шаг, если он свёрнут. Клик идёт в заголовок шага и только
        по свёрнутому: повторный клик свернул бы шаг, раскрытый раньше, а конец
        хода сворачивает контейнер процесса сам. Шаг без содержимого фронт
        рисует строкой без раскрытия — раскрывать нечего."""
        toggle = step.locator(Selector.STEP_TOGGLE.value).first
        if not toggle.count():
            return

        if toggle.get_attribute(Selector.TOGGLE_STATE.value) == Selector.OPENED.value:
            return

        toggle.click()
        self.page.wait_for_timeout(300)

    def await_step(self, step_type: str, timeout_ms: float | None = None) -> Locator:
        """Ждёт первый шаг указанного типа и отдаёт его локатор."""
        selector = Selector.of_type(step_type)
        self._await(selector, timeout_ms)
        return self.page.locator(selector).first

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

        msg = (
            f"chat page: no task_end after task_start within {timeout_sec}s, "
            f"the turn is still running\n{self.log.describe()}"
        )
        raise ChatPageError(msg)

    def await_event(self, event: ChatEvent, timeout_sec: float = 30.0) -> None:
        """Ждёт событие сервера в журнале вкладки.

        Действие, о конце которого страница ничем не сообщает в разметке
        (сохранение настроек, перезапуск агента), тест узнаёт по событию,
        которым сервер его завершает. Журнал тест очищает перед действием,
        чтобы не принять за ответ событие, пришедшее раньше.
        """
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if self.log.of_event(event):
                return

            self.page.wait_for_timeout(100)

        msg = (
            f"chat page: no {event.value!r} event in the socket log within "
            f"{timeout_sec}s\n{self.log.describe()}"
        )
        raise ChatPageError(msg)

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
            msg = f"selector {selector!r} is not attached within {wait}ms: {exc}"
            raise ChatPageError(msg) from exc

    def _profile_selector(self) -> Locator:
        # список профилей приходит отдельным запросом после отрисовки страницы
        try:
            self._await(Selector.PROFILES.value)
        except ChatPageError as exc:
            msg = self._missing("profile selector", Selector.PROFILES.value)
            raise ChatPageError(f"{msg}\n{exc}") from exc

        return self.page.locator(Selector.PROFILES.value)

    def _missing(self, what: str, selector: str) -> str:
        return f"{what} is not drawn by {selector!r}\n{self.dom()[:2000]}"
