"""Вопрос агента пользователю в живом чате: панель вместо поля ввода и
запись ответа в ленте.

Фейковая модель зовёт собственный инструмент агента AskUserQuestion, агент
шлёт чату форму `elicitation/create`, чат показывает её панелью элемента
Question вместо поля ввода. Проверяется то, что видит пользователь: панель
по каждому состоянию макета в обеих темах (цвета по токенам темы, кегль,
геометрия), клавиатура, ответ, отказ, отмена остановкой хода, придержанная
панель, пока пользователь печатает, повторный показ после перезагрузки
страницы и запись ответа в основной ленте (без раскрытия шагов) и в
истории. Снимки состояний — в рабочий каталог стенда (question-look), их
сверяют с макетом глазами.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Locator, Page

from boba.stand.ui.chat_page import ChatPage
from boba.stand.ui.fake_llm import FakeRoute, ScenarioName
from boba.stand.ui.stand import StandUrl

pytestmark = [pytest.mark.ui, pytest.mark.xdist_group("question-ui")]

PANEL = "#question-dock [data-question-panel]"
COMPOSER = "#message-composer"
SUBMIT = "[data-submit]"
HELD = "[data-question-held]"
ROWS = '[role="radio"], [role="checkbox"]'
ANSWER_AFTER_TOOL = "the tool has answered"
WAITING = "[data-question-waiting]"
MESSAGE_OF = "xpath=ancestor::*[@data-step-type][1]"
SUCCESS_DOT = "rgb(116, 201, 145)"
FAILURE_DOT = "rgb(199, 78, 57)"
LOCK_MS = 600

DB: dict[str, Any] = {
    "question": "Which database should the service use?",
    "header": "Database",
    "options": [
        {"label": "PostgreSQL", "description": ""},
        {"label": "ClickHouse", "description": ""},
        {"label": "Oracle", "description": ""},
    ],
}
APPROACH: dict[str, Any] = {
    "question": "How should I refactor the export module?",
    "header": "Approach",
    "options": [
        {
            "label": "Split by format (Recommended)",
            "description": (
                "One reader per format behind a router; the callers keep the "
                "same entry point."
            ),
        },
        {
            "label": "Keep one module",
            "description": (
                "Only extract the duplicated parsing into helpers; the smallest diff."
            ),
        },
        {
            "label": "Rewrite on pumps",
            "description": (
                "Move export onto the ETL pumps; changes the callers and the tests."
            ),
        },
    ],
}
CHECKS: dict[str, Any] = {
    "question": "Which checks should run before the release?",
    "header": "Checks",
    "multiSelect": True,
    "options": [
        {
            "label": "Unit tests",
            "description": "Fast package tests, about two minutes.",
        },
        {"label": "UI tests", "description": "Playwright suite on the aid stand."},
        {"label": "Load test", "description": "Feed throughput on the perf stand."},
        {"label": "Security scan", "description": "SAST over the changed packages."},
    ],
}
AUTH: dict[str, Any] = {
    "question": "How should users sign in?",
    "header": "Auth",
    "options": [
        {
            "label": "Kerberos SSO",
            "description": "Ticket from the browser, no password prompt.",
        },
        {
            "label": "OAuth (Keycloak)",
            "description": "Redirect to the identity provider.",
        },
        {"label": "Password", "description": "Local accounts in the chat database."},
    ],
}
DEPLOY: dict[str, Any] = {
    "question": "Where should it be deployed first?",
    "header": "Deploy",
    "options": [
        {"label": "Dev stand", "description": "Rootless docker on the dev host."},
        {"label": "Staging", "description": "Shared stand of the team."},
    ],
}
LAYOUT: dict[str, Any] = {
    "question": "Which layout should the report page use?",
    "header": "Layout",
    "options": [
        {
            "label": "Single column",
            "description": "Filters above the table.",
            "preview": (
                "+--------------------------------+\n"
                "| Filters: [date] [region] [Go]  |\n"
                "+--------------------------------+\n"
                "| Table                          |\n"
                "| ...                            |\n"
                "| ...                            |\n"
                "+--------------------------------+"
            ),
        },
        {
            "label": "Two columns",
            "description": "Filters in a side panel.",
            "preview": (
                "+---------+----------------------+\n"
                "| Filters | Table                |\n"
                "| [date]  | ...                  |\n"
                "| [region]| ...                  |\n"
                "| [Go]    | ...                  |\n"
                "+---------+----------------------+"
            ),
        },
        {
            "label": "Tabs",
            "description": "One tab per region.",
            "preview": (
                "[ North ][ South ][ West ]\n"
                "+--------------------------------+\n"
                "| Table of the selected region   |\n"
                "| ...                            |\n"
                "+--------------------------------+"
            ),
        },
    ],
}
NAME: dict[str, Any] = {
    "question": "What should the new schema be called?",
    "header": "Name",
    "options": [
        {"label": "analytics", "description": "Generic name used by the BI team."},
        {"label": "reporting", "description": "Matches the report service."},
        {"label": "dwh", "description": "Short, as in the old cluster."},
    ],
}


@dataclass(frozen=True)
class QuestionLabels:
    """Подписи панели и записи вопроса (components.Question) из переводов,
    которые отдаёт сам сервер чата на языке его настроек."""

    texts: Mapping[str, Any]

    @property
    def other(self) -> str:
        return str(self.texts["other"])

    @property
    def submit(self) -> str:
        return str(self.texts["submit"])

    @property
    def cancel_hint(self) -> str:
        return str(self.texts["cancelHint"])

    @property
    def held(self) -> str:
        return str(self.texts["held"])

    def status(self, state: str) -> str:
        return str(self.texts["status"][state])

    def title(self, count: int) -> str:
        if count == 1:
            return str(self.texts["title"]["one"])

        return str(self.texts["title"]["many"])

    def summary(self, state: str, count: int) -> str:
        """Строка статуса записи для одного или двух вопросов: форма
        множественного числа «few» есть не во всех языках."""
        forms = ["questions_other"]
        if count == 1:
            forms = ["questions_one"]

        if count == 2:
            forms = ["questions_few", "questions_other"]

        for form in forms:
            if form in self.texts:
                counted = str(self.texts[form]).replace("{{count}}", str(count))
                return f"{self.status(state)} · {counted}"

        msg = f"the translations carry none of {forms}: {self.texts}"
        raise AssertionError(msg)


class QuestionPage:
    """Вкладка чата с вопросом агента: вопрос моделью, панель, запись ответа
    и то, что модель получила в ответ."""

    def __init__(self, chat: ChatPage, llm_port: int) -> None:
        self.chat = chat
        self.page: Page = chat.page
        self._llm_port = llm_port
        reply = httpx.get(f"{chat.base_url}/project/translations", timeout=10.0)
        reply.raise_for_status()
        texts = reply.json()["translation"]["components"]["Question"]
        self.labels = QuestionLabels(texts)

    def ask(self, *questions: Mapping[str, Any]) -> Locator:
        """Ход, в котором модель зовёт AskUserQuestion; итог — панель."""
        arguments = {"questions": list(questions)}
        request = json.dumps({"name": "AskUserQuestion", "arguments": arguments})
        self.chat.ask(f"{ScenarioName.CALL.value} {request}")

        return self.panel()

    def panel(self) -> Locator:
        panel = self.page.locator(PANEL)
        panel.wait_for(state="visible", timeout=60_000)
        # первые полсекунды панель не принимает Enter, 1 и Esc
        self.page.wait_for_timeout(LOCK_MS)

        return panel

    def row(self, label: str) -> Locator:
        return self.page.locator(PANEL).locator(ROWS).filter(has_text=label).first

    def submit(self) -> None:
        self.page.locator(PANEL).locator(SUBMIT).click()

    def record(self, state: str, answered: bool = True) -> Locator:
        """Запись ответа в основной ленте — видна сразу, без раскрытия
        контейнера хода и шагов; answered — ход дошёл до ответа модели после
        инструмента."""
        if answered:
            self.await_answer()

        record = self.page.locator(f'[data-question-review="{state}"]').last
        record.wait_for(state="visible", timeout=30_000)
        message = record.locator(MESSAGE_OF)
        assert message.get_attribute("data-step-type") == "assistant_message"

        return record

    def reload(self) -> None:
        """Перезагрузка треда: лента — история агента после ответа модели."""
        self.page.goto(self.thread_url(), wait_until="domcontentloaded")
        self.await_answer()

    def asked_while_typing(self, text: str) -> None:
        """Вопрос приходит, пока пользователь печатает: ход с вопросом и
        набор text в поле ввода сразу за ним. Вопрос, обогнавший первую букву
        (стенд под нагрузкой), отклоняется и задаётся снова."""
        request = json.dumps(
            {"name": "AskUserQuestion", "arguments": {"questions": [DB]}}
        )
        held = self.page.locator(HELD)
        for _ in range(3):
            self.chat.ask(f"{ScenarioName.CALL.value} {request}")
            self.page.locator("#chat-input").focus()
            if self._typed(text, held):
                return

            self.panel()
            self.press("Escape")
            self.record("decline")

        msg = "the question came before the first letter three times in a row"
        raise AssertionError(msg)

    def _typed(self, text: str, held: Locator) -> bool:
        """Набор text по букве; False — панель встала раньше подсказки."""
        seen = False
        for char in text:
            if not seen and self.page.locator(PANEL).count():
                return False

            self.page.keyboard.type(char)
            self.page.wait_for_timeout(120)
            if not held.count():
                continue

            seen = True
            assert self.page.locator(COMPOSER).is_visible()
            assert self.page.locator(PANEL).count() == 0

        return seen

    def thread_url(self) -> str:
        return f"{self.chat.base_url}/thread/{self.chat.log.thread_id()}"

    def active_tab(self) -> str:
        return self.page.locator(PANEL).locator(".navTabActive").inner_text()

    def focused_label(self) -> str:
        """Метка строки варианта в фокусе; пусто — фокус не на строке."""
        return str(
            self.page.evaluate(
                "() => { const row = document.activeElement;"
                " const label = row && row.querySelector"
                " && row.querySelector('.optionLabel');"
                " return label ? label.textContent : ''; }"
            )
        )

    def press(self, key: str) -> None:
        self.page.keyboard.press(key)
        self.page.wait_for_timeout(80)

    def theme(self, dark: bool) -> None:
        self.page.evaluate(
            "(dark) => document.documentElement.classList.toggle('dark', dark)", dark
        )
        self.page.wait_for_timeout(150)

    def shoot(self, folder: Path, scene: str) -> None:
        """Снимок вкладки в обеих темах: <scene>-light.png и <scene>-dark.png."""
        folder.mkdir(parents=True, exist_ok=True)
        # наведение мыши подсвечивает строку под курсором — снимок без него
        self.page.mouse.move(1, 450)
        self.page.wait_for_timeout(200)
        for dark, theme in ((False, "light"), (True, "dark")):
            self.theme(dark)
            self.page.screenshot(path=str(folder / f"{scene}-{theme}.png"))

        self.theme(False)

    def color(self, css: str) -> str:
        """Вычисленный цвет выражения CSS на токенах темы страницы."""
        return str(
            self.page.evaluate(
                "(css) => { const probe = document.createElement('div');"
                " probe.style.color = css; document.body.appendChild(probe);"
                " const value = getComputedStyle(probe).color; probe.remove();"
                " return value; }",
                css,
            )
        )

    def await_answer(self) -> None:
        """Ход дошёл до ответа модели после инструмента."""
        self.page.get_by_text(ANSWER_AFTER_TOOL).last.wait_for(timeout=60_000)

    def tool_result(self) -> str:
        """Что модель получила итогом вызова вопроса: сообщение tool её
        последнего запроса."""
        reply = httpx.get(StandUrl.of(self._llm_port, FakeRoute.REQUESTS.value))
        requests: Sequence[Mapping[str, Any]] = reply.json()["requests"]
        for message in reversed(requests[-1]["messages"]):
            if message.get("role") == "tool":
                return str(message.get("content"))

        msg = f"the last model request carries no tool result: {requests[-1]}"
        raise AssertionError(msg)


@pytest.fixture
def asked(chat: ChatPage, llm_port: int) -> QuestionPage:
    return QuestionPage(chat, llm_port)


@pytest.fixture(scope="session")
def look_dir(stand_workdir: Path) -> Path:
    """Снимки состояний панели и записи для сверки с макетом глазами."""
    return stand_workdir / "question-look"


def css(locator: Locator, prop: str) -> str:
    return str(locator.evaluate("(node, prop) => getComputedStyle(node)[prop]", prop))


class TestAnswer:
    """Ответ панели доходит до модели, ход продолжается, запись — в ленте."""

    def test_answer_reaches_the_model(self, asked: QuestionPage) -> None:
        panel = asked.ask(DB)

        if asked.page.locator(COMPOSER).is_visible():
            raise AssertionError("the composer stays hidden while the panel is shown")

        submit = panel.locator(SUBMIT)
        if not submit.is_disabled():
            raise AssertionError("Submit waits for an answer")

        asked.row("ClickHouse").click()
        asked.submit()
        record = asked.record("accept")

        if not asked.page.locator(COMPOSER).is_visible():
            raise AssertionError("the composer is back after the answer")

        status = record.locator(".statusLine").inner_text()
        if not status.startswith(asked.labels.summary("accept", 1)):
            raise AssertionError(f"the record status: {status!r}")

        checked = record.locator('[aria-checked="true"]').inner_text()
        if "ClickHouse" not in checked:
            raise AssertionError(f"the record checks the answer: {checked!r}")

        result = asked.tool_result()
        expected = '"Which database should the service use?"="ClickHouse"'
        if expected not in result:
            raise AssertionError(f"the model got the answer: {result!r}")

    def test_typed_answers_and_several_choices(self, asked: QuestionPage) -> None:
        """Несколько флажков и «Other» со своим текстом: свой текст
        последним, модель получает второй текст ответов, запись отмечает
        выбранное и показывает свой текст под Other."""
        asked.ask(CHECKS)

        asked.row("Unit tests").click()
        asked.row("Security scan").click()
        asked.row(asked.labels.other).click()
        asked.page.keyboard.type("smoke test on the stand")
        asked.submit()
        record = asked.record("accept")

        checked: list[str] = []
        for row in record.locator('[aria-checked="true"] .optionLabel').all():
            checked.append(row.inner_text())

        assert checked == ["Unit tests", "Security scan", asked.labels.other]
        assert "smoke test on the stand" in record.locator(".typedAnswer").inner_text()
        dimmed = record.locator(".optionDimmed").count()
        assert dimmed == 2, dimmed

        result = asked.tool_result()
        assert result.startswith(
            'The user answered: "Which checks should run before the release?"='
            '"Unit tests, Security scan, smoke test on the stand".'
        ), result


class TestKeyboard:
    """Клавиатура панели: стрелки, Enter, пробел, автопереход, цифра 1, Esc."""

    def test_arrows_enter_space_and_submit(self, asked: QuestionPage) -> None:
        asked.ask(DB, AUTH, DEPLOY)

        assert asked.focused_label() == "PostgreSQL"
        asked.press("ArrowDown")
        assert asked.focused_label() == "ClickHouse"
        asked.press("ArrowUp")
        asked.press("ArrowUp")
        assert asked.focused_label() == asked.labels.other

        asked.press("ArrowDown")
        asked.press("ArrowDown")
        asked.press("Enter")
        asked.page.wait_for_timeout(450)
        assert asked.active_tab() == "Auth"
        assert asked.focused_label() == "Kerberos SSO"

        asked.press("ArrowLeft")
        assert asked.active_tab() == "Database"
        checked = asked.page.locator(PANEL).locator('[aria-checked="true"]')
        assert checked.inner_text().startswith("ClickHouse")

        asked.press("ArrowRight")
        asked.press(" ")
        asked.page.wait_for_timeout(450)
        assert asked.active_tab() == "Deploy"

        asked.press("1")
        assert asked.page.locator(PANEL).is_visible(), "1 waits for every answer"

        asked.press("ArrowDown")
        asked.press("Enter")
        assert asked.page.locator(PANEL).is_visible(), "the first Enter only picks"

        asked.press("Enter")
        record = asked.record("accept")

        checked_labels: list[str] = []
        for row in record.locator('[aria-checked="true"] .optionLabel').all():
            checked_labels.append(row.inner_text())

        assert checked_labels == ["ClickHouse", "Kerberos SSO", "Staging"]
        result = asked.tool_result()
        assert '"How should users sign in?"="Kerberos SSO"' in result, result

    def test_digit_one_submits(self, asked: QuestionPage) -> None:
        asked.ask(APPROACH)

        asked.row("Keep one module").click()
        asked.press("1")
        asked.record("accept")

        assert '="Keep one module"' in asked.tool_result()

    def test_escape_declines(self, asked: QuestionPage) -> None:
        """Esc — отказ: модели сказано остановиться, запись Declined без
        выбора и без приглушения."""
        asked.ask(DB)

        asked.press("Escape")
        record = asked.record("decline")

        status = record.locator(".statusLine").inner_text()
        assert status.startswith(asked.labels.status("decline")), status
        assert record.locator('[aria-checked="true"]').count() == 0
        assert record.locator(".optionDimmed").count() == 0
        assert "The user doesn't want to proceed" in asked.tool_result()

    def test_other_box_keeps_escape_and_digits(self, asked: QuestionPage) -> None:
        """В поле «Other» Esc и цифры — текст, а не команды панели."""
        asked.ask(NAME)

        asked.row(asked.labels.other).click()
        asked.page.keyboard.type("v1")
        asked.press("Escape")
        assert asked.page.locator(PANEL).is_visible()

        asked.page.locator(PANEL).locator(SUBMIT).click()
        asked.record("accept")
        assert '="v1"' in asked.tool_result()


class TestWithoutTheUser:
    """Вопрос снят без пользователя: остановка хода, перезагрузка вкладки."""

    def test_stop_from_another_tab_cancels_the_question(
        self, asked: QuestionPage
    ) -> None:
        asked.ask(DB)
        thread_url = asked.thread_url()
        second = asked.page.context.new_page()
        try:
            second.goto(thread_url, wait_until="domcontentloaded")
            stop = second.locator("#stop-button")
            stop.wait_for(state="visible", timeout=60_000)
            # вопрос остаётся в первой вкладке: она жива
            assert asked.page.locator(PANEL).is_visible()
            assert second.locator(PANEL).count() == 0
            stop.click()

            asked.page.locator(PANEL).wait_for(state="detached", timeout=30_000)
            record = asked.record("cancel", answered=False)
        finally:
            second.close()

        status = record.locator(".statusLine").inner_text()
        assert status.startswith(asked.labels.status("cancel")), status
        assert asked.page.locator(COMPOSER).is_visible()

    def test_reload_shows_the_question_again(self, asked: QuestionPage) -> None:
        asked.ask(DB)
        thread_url = asked.thread_url()

        asked.page.goto(thread_url, wait_until="domcontentloaded")
        asked.panel()
        asked.row("Oracle").click()
        asked.submit()
        asked.record("accept")

        assert '="Oracle"' in asked.tool_result()


class TestHistory:
    def test_record_survives_the_reload(self, asked: QuestionPage) -> None:
        """История после перезагрузки рисует ту же запись тем же кодом."""
        asked.ask(DB, CHECKS)
        asked.row("PostgreSQL").click()
        asked.page.wait_for_timeout(450)
        asked.row("Unit tests").click()
        asked.submit()
        asked.record("accept")

        asked.reload()
        record = asked.record("accept")

        assert (
            record.locator(".statusLine")
            .inner_text()
            .startswith(asked.labels.summary("accept", 2))
        )
        checked: list[str] = []
        for row in record.locator('[aria-checked="true"] .optionLabel').all():
            checked.append(row.inner_text())

        assert checked == ["PostgreSQL", "Unit tests"]
        assert record.locator(".toolNameText").inner_text() == asked.labels.title(2)
        assert css(record.locator(".q-dot"), "backgroundColor") == SUCCESS_DOT


class TestHeldWhileTyping:
    def test_panel_waits_until_typing_stops(self, asked: QuestionPage) -> None:
        """Пока поле ввода с текстом правят чаще раза в 1,5 с, панель
        придержана и над полем подсказка; затихли — панель встаёт, черновик
        цел."""
        # тред с прошлым ходом: перепривязка шага нового вопроса не оставляет
        # ленту пустой, и поле ввода не пересоздаётся посреди набора
        asked.chat.ask(ScenarioName.ANSWER.value)
        asked.chat.await_idle()
        asked.asked_while_typing("Actually, use the existing cluster")
        quiet_at = time.monotonic()
        asked.page.locator(PANEL).wait_for(state="visible", timeout=10_000)
        waited = time.monotonic() - quiet_at
        assert waited > 1.0, waited
        assert not asked.page.locator(COMPOSER).is_visible()

        asked.page.wait_for_timeout(LOCK_MS)
        asked.row("PostgreSQL").click()
        asked.submit()
        asked.record("accept")
        draft = asked.page.locator("#chat-input").input_value()
        assert draft == "Actually, use the existing cluster", draft


class TestLook:
    """Каждое состояние макета живым чатом: цвета по токенам темы, кегль
    13px, геометрия; снимки в обеих темах — в question-look стенда."""

    def _composer_width(self, asked: QuestionPage) -> float:
        box = asked.page.locator(COMPOSER).bounding_box()
        assert box is not None
        return box["width"]

    def test_01_single_question_three_options(
        self, asked: QuestionPage, look_dir: Path
    ) -> None:
        width = self._composer_width(asked)
        panel = asked.ask(DB)

        root = panel
        assert css(root, "fontSize") == "13px"
        container = panel.locator(".permissionRequestContainer")
        box = container.bounding_box()
        assert box is not None
        assert abs(box["width"] - width) <= 2, (box, width)
        radio = panel.locator(".radio").first
        assert css(radio, "borderTopColor") == asked.color(
            "hsl(var(--muted-foreground) / 0.45)"
        )
        assert css(radio, "width") == "16px"
        focused = panel.locator(".option").first
        assert css(focused, "backgroundColor") == asked.color("hsl(var(--muted))")
        submit = panel.locator(SUBMIT)
        assert submit.is_disabled()
        assert css(submit, "opacity") == "0.5"
        assert panel.locator(".navTab").all_inner_texts() == ["Database"]
        hints = panel.locator(".keyboardHints").inner_text()
        assert hints == asked.labels.cancel_hint, hints
        label = submit.inner_text().replace("\n", " ")
        assert label.startswith(f"1 {asked.labels.submit}"), label
        # 13px панели × 1.1 содержимого × 0.9 метки
        assert css(panel.locator(".optionLabel").first, "fontSize") == "12.87px"
        waiting = asked.page.locator(WAITING)
        assert waiting.count() == 1
        assert waiting.locator(".toolNameText").inner_text() == asked.labels.title(1)
        assert css(waiting.locator(".q-dot"), "animationName") == "q-blink"
        message = waiting.locator(MESSAGE_OF)
        assert message.get_attribute("data-step-type") == "assistant_message"

        asked.shoot(look_dir, "01-single-3-options")

    def test_02_options_with_descriptions(
        self, asked: QuestionPage, look_dir: Path
    ) -> None:
        panel = asked.ask(APPROACH)
        asked.row("Split by format (Recommended)").click()

        primary = asked.color("hsl(var(--primary))")
        radio = panel.locator(".radioChecked")
        assert css(radio, "borderTopColor") == primary
        assert css(panel.locator(".radioDot"), "backgroundColor") == primary
        submit = panel.locator(SUBMIT)
        assert submit.is_enabled()
        assert css(submit, "backgroundColor") == primary
        assert css(submit, "fontWeight") == "700"
        description = panel.locator(".optionDescription").first
        assert css(description, "color") == asked.color("hsl(var(--muted-foreground))")

        asked.shoot(look_dir, "02-option-descriptions")

    def test_03_multiple_choice(self, asked: QuestionPage, look_dir: Path) -> None:
        panel = asked.ask(CHECKS)
        asked.row("Unit tests").click()
        asked.row("Load test").click()

        boxes = panel.locator(".checkbox")
        assert boxes.count() == 5
        checked = panel.locator(".checkboxChecked")
        assert checked.count() == 2
        assert css(checked.first, "backgroundColor") == asked.color(
            "hsl(var(--primary))"
        )
        assert css(boxes.first, "borderTopLeftRadius") == "3px"
        assert "navTabAnswered" in str(
            panel.locator(".navTab").first.get_attribute("class")
        )
        assert asked.focused_label() == "Load test"

        asked.shoot(look_dir, "03-multi-select")

    def test_04_three_questions_as_tabs(
        self, asked: QuestionPage, look_dir: Path
    ) -> None:
        panel = asked.ask(DB, AUTH, DEPLOY)
        asked.row("PostgreSQL").click()
        asked.page.wait_for_timeout(450)

        tabs = panel.locator(".navTab")
        assert tabs.all_inner_texts() == ["Database", "Auth", "Deploy"]
        active = panel.locator(".navTabActive")
        assert active.inner_text() == "Auth"
        assert css(active, "borderBottomColor") == asked.color("hsl(var(--primary))")
        answered = panel.locator(".navTabAnswered")
        assert answered.inner_text() == "Database"
        assert css(answered, "color") == asked.color("hsl(var(--muted-foreground))")
        assert panel.locator(SUBMIT).is_disabled()

        asked.shoot(look_dir, "04-three-tabs")

    def test_05_preview_side_by_side(self, asked: QuestionPage, look_dir: Path) -> None:
        panel = asked.ask(LAYOUT)
        asked.row("Two columns").click()

        pane = panel.locator(".previewPane")
        options = panel.locator(".questionsContainer")
        pane_box = pane.bounding_box()
        options_box = options.bounding_box()
        assert pane_box is not None
        assert options_box is not None
        assert pane_box["x"] > options_box["x"] + options_box["width"] - 1
        assert panel.locator(".previewCaption").inner_text() == "Two columns"
        assert "| Filters | Table" in panel.locator(".previewBody").inner_text()
        highlighted = panel.locator(".optionHighlighted")
        assert highlighted.locator(".optionLabel").inner_text() == "Two columns"
        body = panel.locator(".previewBody")
        assert css(body, "fontFamily").endswith("monospace")
        assert css(body, "fontSize") == "13px"
        assert css(pane, "backgroundColor") == asked.color("hsl(var(--muted))")

        asked.shoot(look_dir, "05-preview")

        asked.page.set_viewport_size({"width": 420, "height": 900})
        asked.page.wait_for_timeout(300)
        narrow_pane = pane.bounding_box()
        narrow_options = options.bounding_box()
        assert narrow_pane is not None
        assert narrow_options is not None
        assert narrow_pane["y"] >= narrow_options["y"] + narrow_options["height"] - 1
        scroll = asked.page.evaluate(
            "[document.documentElement.scrollWidth,"
            " document.documentElement.clientWidth]"
        )
        assert scroll[0] <= scroll[1], scroll

    def test_06_other_with_free_text(self, asked: QuestionPage, look_dir: Path) -> None:
        panel = asked.ask(NAME)
        asked.row(asked.labels.other).click()
        asked.page.keyboard.type("sales_mart")

        box = panel.locator(".otherInput")
        assert css(box, "borderTopColor") == asked.color("hsl(var(--ring))")
        assert css(box, "backgroundColor") == asked.color("hsl(var(--background))")
        assert panel.locator(".q-input").inner_text() == "sales_mart"
        assert panel.locator(SUBMIT).is_enabled()

        asked.shoot(look_dir, "06-other-free-text")

    def test_07_answered_record(self, asked: QuestionPage, look_dir: Path) -> None:
        asked.ask(DB, CHECKS)
        asked.row("PostgreSQL").click()
        asked.page.wait_for_timeout(450)
        asked.row("Unit tests").click()
        asked.row("Security scan").click()
        asked.row(asked.labels.other).click()
        asked.page.keyboard.type("smoke test on the stand")
        asked.submit()
        record = asked.record("accept")

        assert asked.page.locator(WAITING).count() == 0
        assert css(record, "fontSize") == "13px"
        assert record.locator(".toolNameText").inner_text() == asked.labels.title(2)
        dot = record.locator(".q-dot")
        assert css(dot, "backgroundColor") == SUCCESS_DOT
        assert css(dot, "animationName") == "none"
        body = record.locator(".toolBody")
        assert css(body, "borderTopColor") == asked.color(
            "hsl(var(--muted-foreground) / 0.45)"
        )
        assert css(body, "borderTopLeftRadius") == "5px"
        dimmed = record.locator(".optionDimmed").first
        assert css(dimmed, "opacity") == "0.6"
        assert (
            record.locator(".statusLine")
            .inner_text()
            .startswith(asked.labels.summary("accept", 2))
        )
        record.scroll_into_view_if_needed()

        asked.shoot(look_dir, "07-answered-feed-history")

        record.locator(".secondaryLine").click()
        assert record.locator(".toolBody").count() == 0

        asked.reload()
        record = asked.record("accept")
        assert record.locator(".optionDimmed").count() == 4
        assert record.locator(".typedAnswer").inner_text() == "smoke test on the stand"
        record.scroll_into_view_if_needed()

        asked.shoot(look_dir, "07-answered-feed-history-reload")

    def test_08_declined_record(self, asked: QuestionPage, look_dir: Path) -> None:
        asked.ask(DB)
        asked.page.locator(PANEL).locator("[data-close]").click()
        record = asked.record("decline")

        assert record.locator(".optionDimmed").count() == 0
        assert record.locator(".radioChecked").count() == 0
        assert record.locator(".toolNameText").inner_text() == asked.labels.title(1)
        assert css(record.locator(".q-dot"), "backgroundColor") == FAILURE_DOT
        status = record.locator(".statusLine").inner_text()
        assert status.startswith(asked.labels.status("decline")), status
        record.scroll_into_view_if_needed()

        asked.shoot(look_dir, "08-declined")

        asked.reload()
        record = asked.record("decline")
        assert css(record.locator(".q-dot"), "backgroundColor") == FAILURE_DOT
        record.scroll_into_view_if_needed()

        asked.shoot(look_dir, "08-declined-reload")

    def test_10_held_while_typing(self, asked: QuestionPage, look_dir: Path) -> None:
        asked.chat.ask(ScenarioName.ANSWER.value)
        asked.chat.await_idle()
        asked.asked_while_typing("Actually, use the existing clu")
        held = asked.page.locator(HELD)
        hint = held.locator(".q-hint")
        assert hint.inner_text() == asked.labels.held
        assert css(hint, "color") == asked.color("hsl(var(--muted-foreground))")
        for dark, theme in ((False, "light"), (True, "dark")):
            asked.theme(dark)
            asked.page.keyboard.type(" ")
            look_dir.mkdir(parents=True, exist_ok=True)
            asked.page.screenshot(
                path=str(look_dir / f"10-held-while-typing-{theme}.png")
            )

        asked.theme(False)
        asked.panel()
        asked.press("Escape")
        asked.record("decline")
