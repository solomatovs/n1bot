"""Вид хода из многих шагов: контейнер хода на каждом этапе — размышление,
удачный вызов, идущий вызов, вызов с ошибкой, законченный ход и он же после
перезагрузки; и ввод во время такого хода — Send, вопрос в очереди под ним,
ход, закрытый там, где агент вставил вопрос, продолжение хода под вопросом
(и то же после перезагрузки) и Stop с возвратом текста в поле. Обе темы,
1280 и 420.

Шаги дают инструменты обычного сервера MCP стенда (standard_server.py):
add отвечает сразу, sleep_for держит вызов идущим, broken отказывает. Поток
модели книга ответов держит на паузе, пока тест снимает кадр этапа.
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from typing import ClassVar

import pytest
from acp_stand import standard
from aid_look import (
    LookCheck,
    LookFix,
    LookPages,
    LookSize,
    LookStand,
    LookState,
    LookTheme,
)
from playwright.sync_api import Locator, Page, expect
from test_look_conversation import TalkFeed, TalkFixes, TalkLook, talk_book
from test_look_queued import QueuedLook

from boba.mcp_client.client import McpServerConfig
from boba.stand.ui.chat_page import Selector, StepKind
from boba.stand.ui.fake_llm import Reply, ReplyBookClient, ReplyCall, ReplyTurn
from boba.stand.ui.stand import StandProcess

pytestmark = [pytest.mark.ui, pytest.mark.look]

__all__ = ["standard", "talk_book"]


class StepsQuestion(StrEnum):
    """Вопросы ходов из многих шагов: ключи книги ответов."""

    CHECK = "Проверь выгрузку по шагам"
    QUEUE_FIRST = "Сверь витрину по шагам"
    QUEUED = "А сколько строк в витрине?"
    STOPPED = "Пересчитай витрину по шагам"
    LATER = "Потом проверь индексы"


class StepsText(StrEnum):
    """Тексты ответов книги."""

    THINKING = (
        "Сначала сложу счётчики двух выгрузок, затем подожду, пока витрина "
        "досчитается, и проверю таблицу заказов."
    )
    LOOKING = "Складываю счётчики."
    WAITING = "Жду пересчёта витрины."
    CHECKING = "Проверяю таблицу заказов."
    DONE = (
        "Счётчики сошлись, витрина пересчитана, а таблицы заказов нет — её "
        "нужно создать."
    )
    COUNTED = "Считаю строки."
    ROWS = "В витрине пять строк."


class StepsBook:
    """Книга ходов: размышление с загрузкой инструментов (ToolSearch) → add →
    sleep_for → broken → ответ. Паузы: после десяти слов размышления и перед
    вызовом sleep_for (add уже закончен)."""

    THINKING_TOKENS: ClassVar[int] = 10
    SERVER: ClassVar[str] = "mcp__standard__"
    ADD: ClassVar[str] = "add"
    SLEEP: ClassVar[str] = "sleep_for"
    BROKEN: ClassVar[str] = "broken"
    SEARCH: ClassVar[str] = "ToolSearch"
    SELECT: ClassVar[str] = "select:"
    RUNNING_SEC: ClassVar[float] = 20.0
    """Сколько идёт вызов sleep_for: кадры «идёт» снимаются за это время."""
    STOPPED_SEC: ClassVar[float] = 120.0

    def replies(self) -> list[Reply]:
        return [
            Reply(question=StepsQuestion.CHECK, turns=self._turns(self.RUNNING_SEC)),
            Reply(
                question=StepsQuestion.QUEUE_FIRST,
                turns=self._turns(self.RUNNING_SEC),
            ),
            Reply(question=StepsQuestion.STOPPED, turns=self._turns(self.STOPPED_SEC)),
            Reply(
                question=StepsQuestion.QUEUED,
                turns=[
                    ReplyTurn(content=StepsText.COUNTED, calls=[self._add()]),
                    ReplyTurn(content=StepsText.ROWS),
                ],
            ),
            Reply(
                question=StepsQuestion.LATER,
                turns=[ReplyTurn(content=StepsText.ROWS)],
            ),
        ]

    def _turns(self, seconds: float) -> list[ReplyTurn]:
        sleep = ReplyCall(tool=self.full(self.SLEEP), arguments={"seconds": seconds})
        broken = ReplyCall(tool=self.full(self.BROKEN), arguments={})
        return [
            ReplyTurn(
                reasoning=StepsText.THINKING,
                calls=[self._search()],
                hold=True,
                hold_after_tokens=self.THINKING_TOKENS,
            ),
            ReplyTurn(content=StepsText.LOOKING, calls=[self._add()]),
            ReplyTurn(content=StepsText.WAITING, calls=[sleep], hold=True),
            ReplyTurn(content=StepsText.CHECKING, calls=[broken]),
            ReplyTurn(content=StepsText.DONE),
        ]

    def _search(self) -> ReplyCall:
        """Загрузка отложенных инструментов сервера одним ToolSearch, как
        делает модель, которой их объявили одними именами."""
        names: list[str] = []
        for tool in (self.ADD, self.SLEEP, self.BROKEN):
            names.append(self.full(tool))

        query = f"{self.SELECT}{','.join(names)}"
        return ReplyCall(
            tool=self.SEARCH, arguments={"query": query, "max_results": len(names)}
        )

    def _add(self) -> ReplyCall:
        return ReplyCall(tool=self.full(self.ADD), arguments={"a": 2, "b": 3})

    def full(self, tool: str) -> str:
        return f"{self.SERVER}{tool}"


class StepsFixes:
    """Правки вида, которые показывают кадры многошагового хода."""

    STEPS: ClassVar[LookFix] = LookFix(
        code="Ход из многих шагов",
        summary=(
            "контейнер хода из размышления и трёх вызовов (удачный, идущий, с "
            "ошибкой): свёрнутым — подпись текущего действия на каждом этапе, "
            "раскрытым — все шаги под ним; законченный ход — «Завершено · шагов» "
            "и тот же после перезагрузки"
        ),
    )
    SPLIT: ClassVar[LookFix] = LookFix(
        code="Вопрос посреди хода делит ход",
        summary=(
            "было: вопрос, отправленный во время хода, ждал конца всего хода "
            "и получал свой ход после ответа; стало: агент вставляет его на "
            "ближайшей границе шага — ход в ленте закрывается в этой точке "
            "(«Завершено · шагов: N»), ниже вопрос, под ним продолжение хода с "
            "ответом на него; после перезагрузки лента та же"
        ),
    )


class StepsLook:
    """Состояния хода из многих шагов (группа conversation) и ввода во время
    него (группа queued)."""

    TURN: ClassVar[str] = "conversation"
    QUEUED: ClassVar[str] = QueuedLook.GROUP
    THINKING: ClassVar[LookState] = LookState(
        TURN,
        "ms1-thinking",
        "Многошаговый ход: модель размышляет",
        "контейнер хода свёрнут, подпись — размышление, мерцает; под вопросом "
        "кружок ожидания",
        (StepsFixes.STEPS,),
    )
    CALLED: ClassVar[LookState] = LookState(
        TURN,
        "ms2-called",
        "Многошаговый ход: первый вызов закончен",
        "контейнер свёрнут, подпись — законченный вызов add с галочкой; "
        "текста «Складываю счётчики.» в основной ленте нет — он внутри хода",
        (StepsFixes.STEPS, TalkFixes.INTERIM),
    )
    RUNNING: ClassVar[LookState] = LookState(
        TURN,
        "ms3-running",
        "Многошаговый ход: идёт второй вызов",
        "контейнер свёрнут, подпись — идущий вызов sleep_for, мерцает",
        (StepsFixes.STEPS,),
    )
    RUNNING_OPEN: ClassVar[LookState] = LookState(
        TURN,
        "ms4-running-open",
        "Многошаговый ход раскрыт, пока идёт второй вызов",
        "под контейнером по порядку: размышление, загрузка инструментов "
        "ToolSearch, абзац «Складываю счётчики.», add с галочкой, абзац «Жду "
        "пересчёта витрины.», идущий sleep_for с кружком; абзацы без аватара",
        (StepsFixes.STEPS, TalkFixes.INTERIM),
    )
    FINISHED: ClassVar[LookState] = LookState(
        TURN,
        "ms5-finished",
        "Многошаговый ход закончен, в нём вызов с ошибкой",
        "контейнер свёрнут, подпись «Завершено · шагов: 5»; ниже только "
        "итоговый ответ; кружка нет",
        (StepsFixes.STEPS, TalkFixes.INTERIM),
    )
    FINISHED_OPEN: ClassVar[LookState] = LookState(
        TURN,
        "ms6-finished-open",
        "Законченный многошаговый ход раскрыт",
        "под контейнером в порядке появления: размышление, ToolSearch, "
        "«Складываю счётчики.», add, «Жду пересчёта витрины.», sleep_for с "
        "галочками, «Проверяю таблицу заказов.», broken красным с крестиком",
        (StepsFixes.STEPS, TalkFixes.INTERIM),
    )
    FINISHED_AGAIN: ClassVar[LookState] = LookState(
        TURN,
        "ms5-finished-reloaded",
        "Тот же законченный ход после перезагрузки",
        "кадр тот же, что «Многошаговый ход закончен»; тест сверил его с живым "
        "пиксель в пиксель",
        (StepsFixes.STEPS,),
    )
    SEND: ClassVar[LookState] = LookState(
        QUEUED,
        "qi6-steps-send",
        "Идёт многошаговый ход, в поле набран текст",
        "в ленте идущий ход со свёрнутым контейнером (идёт sleep_for); в поле "
        "текст, справа кнопка Send, кнопки Stop нет",
        (StepsFixes.STEPS,),
    )
    QUEUED_UNDER: ClassVar[LookState] = LookState(
        QUEUED,
        "qi7-steps-queued",
        "Вопрос в очереди под идущим многошаговым ходом",
        "идущий ход со свёрнутым контейнером, под ним пузырь отправленного "
        "вопроса справа; поле пустое, справа Stop",
        (StepsFixes.STEPS,),
    )
    ANSWERED: ClassVar[LookState] = LookState(
        QUEUED,
        "qi8-steps-answered",
        "Вопрос продолжил ход со своими шагами",
        "пузырь вопроса наверху, под ним продолжение хода: свёрнутый "
        "контейнер «Завершено · шагов: 1» (внутри «Считаю строки.» и add) и "
        "ответ «В витрине пять строк.»",
        (StepsFixes.STEPS, StepsFixes.SPLIT, TalkFixes.INTERIM),
    )
    SPLIT_AT: ClassVar[LookState] = LookState(
        QUEUED,
        "qi10-steps-split",
        "Ход закрылся там, где агент вставил вопрос",
        "вопрос первого хода, под ним свёрнутый контейнер «Завершено · шагов: "
        "4» (размышление, ToolSearch, add, sleep_for), ниже пузырь вопроса, "
        "отправленного во время sleep_for, и под ним продолжение хода; "
        "ответа на первый вопрос нет — модель продолжила ход ответом на "
        "второй",
        (StepsFixes.SPLIT,),
    )
    SPLIT_AGAIN: ClassVar[LookState] = LookState(
        QUEUED,
        "qi10-steps-split-reloaded",
        "Тот же разделённый ход после перезагрузки",
        "кадр тот же, что «Ход закрылся там, где агент вставил вопрос»; тест "
        "сверил его с живым пиксель в пиксель",
        (StepsFixes.SPLIT,),
    )
    RESTORED: ClassVar[LookState] = LookState(
        QUEUED,
        "qi9-steps-restored",
        "Stop посреди многошагового хода вернул очередь в поле",
        "ход остановлен посреди sleep_for: контейнер «Остановлено · шагов: 4», "
        "отдельной пометки под ходом нет; вопросов очереди в ленте нет; в поле "
        "два вопроса через пустую строку, справа Send",
        (StepsFixes.STEPS,),
    )
    SEND_TIMEOUT_MS: ClassVar[int] = 30_000
    CHAT: ClassVar[str] = (
        "xpath=ancestor::div[.//div[contains(concat(' ', @class, ' '), "
        "' overflow-y-auto ')]][1]"
    )
    """Колонка чата от поля ввода: лента вместе с полем."""


class StepsFeed:
    """Лента и поле многошагового хода глазами теста вида."""

    def __init__(
        self, feed: TalkFeed, book: ReplyBookClient, queue: QueuedFeed
    ) -> None:
        self._feed = feed
        self._book = book
        self._queue = queue
        self._page = feed.page

    @property
    def page(self) -> Page:
        return self._page

    @property
    def feed(self) -> TalkFeed:
        return self._feed

    @property
    def queue(self) -> QueuedFeed:
        return self._queue

    def asked(self, question: StepsQuestion) -> Locator:
        return self._page.locator(
            Selector.of_type(StepKind.USER.value), has_text=question.value
        ).last

    def region(self, question: StepsQuestion) -> Locator:
        """Лента с кнопкой «↓», прокрученная к вопросу хода."""
        asked = self.asked(question)
        asked.evaluate(LookPages.TOP)
        return asked.locator(TalkLook.FEED)

    def chat(self) -> Locator:
        """Колонка чата: лента и поле ввода."""
        return self._page.locator(TalkLook.COMPOSER_BOX).locator(StepsLook.CHAT)

    def container(self) -> Locator:
        return self._page.locator(Selector.of_type(StepKind.RUN.value)).last

    def ask_held(self, question: StepsQuestion) -> None:
        self._feed.chat.ask(question.value)
        self._book.await_held(question.value, TalkLook.HELD_SEC)

    def release(self, question: StepsQuestion) -> None:
        self._book.release(question.value)

    def held_again(self, question: StepsQuestion) -> None:
        """Отпускает паузу и ждёт следующую паузу того же хода: книга держит
        вопрос один раз, новая книга отпускает идущую паузу и взводит
        следующую."""
        self._book.register(StepsBook().replies())
        self._book.await_held(question.value, TalkLook.HELD_SEC)

    def running(self) -> None:
        """Идёт вызов sleep_for: его подпись в свёрнутом контейнере."""
        expect(self.container()).to_contain_text(
            StepsBook.SLEEP, timeout=StepsLook.SEND_TIMEOUT_MS
        )


class QueuedFeed:
    """Отправка в очередь: Enter в поле и строка лога сервера об очереди."""

    def __init__(self, page: Page, stand: StandProcess) -> None:
        self._page = page
        self._stand = stand

    @property
    def field(self) -> Locator:
        return self._page.locator(Selector.INPUT.value)

    def queued_so_far(self) -> int:
        marker = QueuedLook.QUEUED_LOG.encode()
        found = 0
        with self._stand.log_path.open("rb") as handle:
            for line in handle:
                if marker in line:
                    found += 1

        return found

    def send(self, question: StepsQuestion) -> None:
        before = self.queued_so_far()
        self.field.fill(question.value)
        self.field.press("Enter")
        expect(self.field).to_have_value("")
        page = self._page
        for _ in range(QueuedLook.SETTLE_ROUNDS * 10):
            if self.queued_so_far() > before:
                return

            page.wait_for_timeout(QueuedLook.SETTLE_MS)

        msg = f"{question.value!r} did not reach the agent queue"
        raise AssertionError(msg)


@pytest.fixture(scope="module")
def steps_stand(look_stands: LookStand, standard: McpServerConfig) -> StandProcess:
    server = standard.model_dump(mode="json", exclude_none=True)
    return look_stands.served("steps", {"standard": server}, LookStand.FULL_WINDOW)


def steps_frames(
    look_pages: LookPages, stand: StandProcess, book: ReplyBookClient
) -> Iterator[tuple[LookTheme, LookSize, StepsFeed]]:
    """Сочетания темы и размера: свой вход, свой тред и заново положенная
    книга — паузы потока держат каждый проход."""
    replies = StepsBook().replies()
    for size in LookSize:
        for theme in LookTheme:
            book.register(replies)
            page = look_pages.open(theme, size)
            feed = TalkFeed(page, stand, book)
            look_pages.sign_in(page, stand, TalkLook.LOGIN_USER)
            yield theme, size, StepsFeed(feed, book, QueuedFeed(page, stand))


def test_many_steps_turn_look(
    look: LookCheck,
    look_pages: LookPages,
    steps_stand: StandProcess,
    talk_book: ReplyBookClient,
) -> None:
    for theme, size, steps in steps_frames(look_pages, steps_stand, talk_book):
        page = steps.page
        feed = steps.feed
        question = StepsQuestion.CHECK

        steps.ask_held(question)
        feed.pulse()
        look_pages.settle(page)
        look.frame(steps.region(question), StepsLook.THINKING, theme, size)

        steps.held_again(question)
        feed.folded(StepsText.LOOKING)
        expect(steps.container()).to_contain_text(StepsBook.ADD)
        look_pages.settle(page)
        look.frame(steps.region(question), StepsLook.CALLED, theme, size)

        steps.release(question)
        steps.running()
        look_pages.settle(page)
        look.frame(steps.region(question), StepsLook.RUNNING, theme, size)

        feed.open_last(StepKind.RUN)
        look_pages.settle(page)
        look.frame(steps.region(question), StepsLook.RUNNING_OPEN, theme, size)
        feed.fold_last(StepKind.RUN)

        feed.chat.await_idle(TalkLook.TURN_SEC)
        feed.shown(StepsText.DONE)
        feed.no_pulse()
        look_pages.settle(page)
        look.frame(steps.region(question), StepsLook.FINISHED, theme, size)

        feed.open_last(StepKind.RUN)
        look_pages.settle(page)
        look.frame(steps.region(question), StepsLook.FINISHED_OPEN, theme, size)

        feed.reload()
        feed.shown(StepsText.DONE)
        look_pages.settle(page)
        look.frame(steps.region(question), StepsLook.FINISHED_AGAIN, theme, size)
        look.same_as(StepsLook.FINISHED_AGAIN, StepsLook.FINISHED, theme, size)
        feed.check_clean()

    look.verify()


def test_input_during_many_steps_look(
    look: LookCheck,
    look_pages: LookPages,
    steps_stand: StandProcess,
    talk_book: ReplyBookClient,
) -> None:
    for theme, size, steps in steps_frames(look_pages, steps_stand, talk_book):
        page = steps.page
        feed = steps.feed
        queue = steps.queue
        first = StepsQuestion.QUEUE_FIRST

        steps.ask_held(first)
        steps.held_again(first)
        steps.release(first)
        steps.running()
        queue.field.fill(StepsQuestion.QUEUED.value)
        expect(page.locator(Selector.SUBMIT.value)).to_be_enabled()
        steps.region(first)
        look_pages.settle(page)
        look.frame(steps.chat(), StepsLook.SEND, theme, size)

        queue.field.fill("")
        queue.send(StepsQuestion.QUEUED)
        expect(steps.asked(StepsQuestion.QUEUED)).to_be_visible()
        expect(page.locator(TalkLook.STOP)).to_be_visible()
        steps.region(first)
        look_pages.settle(page)
        look.frame(steps.chat(), StepsLook.QUEUED_UNDER, theme, size)

        feed.chat.await_idle(TalkLook.TURN_SEC)
        feed.shown(StepsText.ROWS)
        feed.no_pulse()
        look_pages.settle(page)
        look.frame(steps.region(StepsQuestion.QUEUED), StepsLook.ANSWERED, theme, size)
        look.frame(steps.region(first), StepsLook.SPLIT_AT, theme, size)

        feed.reload()
        feed.shown(StepsText.ROWS)
        look_pages.settle(page)
        look.frame(steps.region(first), StepsLook.SPLIT_AGAIN, theme, size)
        look.same_as(StepsLook.SPLIT_AGAIN, StepsLook.SPLIT_AT, theme, size)

        stopped = StepsQuestion.STOPPED
        steps.ask_held(stopped)
        steps.held_again(stopped)
        steps.release(stopped)
        steps.running()
        queue.send(StepsQuestion.QUEUED)
        queue.send(StepsQuestion.LATER)
        page.locator(TalkLook.STOP).click()
        feed.chat.await_idle(TalkLook.TURN_SEC)
        restored = f"{StepsQuestion.QUEUED.value}\n\n{StepsQuestion.LATER.value}"
        expect(queue.field).to_have_value(restored, timeout=30_000)
        expect(steps.asked(StepsQuestion.LATER)).to_have_count(0)
        steps.region(stopped)
        look_pages.settle(page)
        look.frame(steps.chat(), StepsLook.RESTORED, theme, size)
        feed.check_clean()

    look.verify()
