"""Инструмент MCP-сервера в живой ленте чата.

Стенд поднимается с MCP-серверами: обычным (официальный SDK, stdio),
который о boba не знает, своим boba-mcp отдельным процессом по HTTP и тем же
обычным сервером по HTTP дважды — с секцией apps (его приложения рисуются в
песочнице на localhost, чат открыт на 127.0.0.1) и без неё.
Серверы получает агент треда (`session/new.mcpServers`); фейковая модель
зовёт их инструменты под полными именами (сервер и имя инструмента на
нём), шаг ленты подписан именем без сервера. Проверяется то, что
видит пользователь: шаг инструмента в ленте, вложение у шага и сам файл по
ссылке вложения, итог своего сервера в его собственной модели результата,
карточка приложения MCP Apps в основной ленте под контейнером хода, её
вид, режимы и изоляция приложения от страницы чата.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

import base64
import json
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar
from urllib.parse import urlencode, urlsplit, urlunsplit

import pytest
from acp_stand import AppsHost, OrdersBook
from chat_ui import BOOT_TIMEOUT_SEC, OpenChat
from playwright.sync_api import (
    Browser,
    Error,
    FloatRect,
    FrameLocator,
    Locator,
    Page,
)

from boba.aid.chat.ui_text import UiText
from boba.mcp_client.client import McpServerConfig
from boba.stand.ui.chat_page import ChatPage, Selector, StepKind
from boba.stand.ui.fake_llm import (
    Reply,
    ReplyBookClient,
    ReplyCall,
    ReplyTurn,
    ScenarioName,
)
from boba.stand.ui.socket_log import ChatEvent, StepField
from boba.stand.ui.stand import (
    REPO_ROOT,
    StandApp,
    StandConfig,
    StandPorts,
    StandProcess,
    StandUrl,
)

pytestmark = [pytest.mark.ui, pytest.mark.xdist_group("mcp-ui")]

CHAT_LOGIN = "admin"
CHAT_ROLE = "ADM"
"""Логин пользователя стенда чата: под ним открыт чат в браузере."""

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
"""Картинка, которую отдаёт инструмент picture стандартного сервера."""


class BobaMcpStand:
    """Сервер boba-mcp отдельным процессом.

    Создаётся фикстурой boba_mcp_stand до стенда чата: чат подключается к нему
    по HTTP от имени пользователя сессии: называет его логин и роли
    заголовками под подписью и получает токен сервиса (вход proxy).
    """

    STAND: Path = (
        REPO_ROOT / "packages" / "apps" / "boba-mcp" / "tests" / "service_stand.py"
    )
    PROXY_SECRET: str = "stand-proxy-secret"
    BOOT_SEC: float = 60.0

    def __init__(self, workdir: Path) -> None:
        self.port = StandPorts().free()
        workdir.mkdir(parents=True, exist_ok=True)
        self._process = subprocess.Popen(
            [sys.executable, str(self.STAND), str(self.port), str(workdir)]
        )

    def await_listening(self) -> None:
        deadline = time.monotonic() + self.BOOT_SEC
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1.0):
                    return
            except OSError:
                time.sleep(0.2)

        msg = f"boba-mcp stand did not start listening on port {self.port}"
        raise AssertionError(msg)

    def server(self) -> dict[str, Any]:
        """Секция [mcp.servers.<имя>] чата для этого сервера."""
        return {
            "connect_timeout_sec": 30.0,
            "call_timeout_sec": 60.0,
            "endpoint": {
                "transport": "streamable-http",
                "scheme": "http",
                "host": "127.0.0.1",
                "port": self.port,
                "path": "/mcp/service",
                "auth": {
                    "auth": "proxy",
                    "issuer": {
                        "scheme": "http",
                        "host": "127.0.0.1",
                        "port": self.port,
                        "path": "/",
                    },
                    "client_id": "stand-chat",
                    "client_secret": "stand-client-secret",
                    "secret": self.PROXY_SECRET,
                },
            },
        }

    def stop(self) -> None:
        self._process.terminate()
        self._process.wait(timeout=10)


@pytest.fixture(scope="module")
def boba_mcp_stand(stand_workdir: Path) -> Iterator[BobaMcpStand]:
    process = BobaMcpStand(stand_workdir / "boba-mcp")
    try:
        process.await_listening()
        yield process
    finally:
        process.stop()


@pytest.fixture(scope="module")
def apps_host(stand_workdir: Path) -> Iterator[AppsHost]:
    host = AppsHost(stand_workdir / "apps-host")
    try:
        host.await_listening()
        yield host
    finally:
        host.stop()


@pytest.fixture(scope="module")
def mcp_stand(  # noqa: PLR0913 — стенд собирается из фикстур модуля
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
    boba_mcp_stand: BobaMcpStand,
    standard: McpServerConfig,
    apps_host: AppsHost,
) -> Iterator[StandProcess]:
    """Стенд чата с MCP-серверами: обычный по stdio, boba-mcp и обычный по
    HTTP — записью apps с секцией apps (страница-прокси песочницы на своём
    источнике) и записью plain без неё."""
    server = standard.model_dump(mode="json", exclude_none=True)
    config = StandConfig(
        # не "mcp": это каталог сервиса boba-mcp общего стенда (StandConfig.service)
        workdir=stand_workdir / "chat-mcp",
        app=StandApp.AID,
        app_port=StandPorts().free(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-mcp",
        mcp_servers={
            "standard": server,
            "boba": boba_mcp_stand.server(),
            "apps": apps_host.drawn(),
            "plain": apps_host.plain(),
        },
    )
    process = StandProcess(config=config, log_path=stand_workdir / "mcp-app.log")
    try:
        process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
        yield process
    finally:
        process.stop()


class FeedCalls:
    """Ход с вызовом инструмента по маркеру сценария фейковой модели: тест
    называет инструмент и аргументы, лента рисует законченный шаг."""

    RUNNING: ClassVar[str] = "○"
    """Кружок идущего вызова в названии шага."""

    def __init__(self, chat: ChatPage) -> None:
        self._chat = chat

    def ask(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Законченный шаг вызова из журнала сокета: конец хода агент
        объявляет, когда итоги вызовов уже в ленте."""
        chat = self._chat
        request = json.dumps({"name": tool, "arguments": arguments})
        chat.ask(f"{ScenarioName.CALL.value} {request}")
        chat.await_idle(timeout_sec=120.0)

        step = chat.log.last_step(StepKind.TOOL.value)
        if step is None:
            raise AssertionError(f"no tool step of {tool}\n{chat.log.describe()}")

        if str(step.get(StepField.NAME.value)).startswith(self.RUNNING):
            raise AssertionError(f"the turn ended with {tool} running: {step!r}")

        return step


class OrdersApp:
    """Приложение заказов сервера apps глазами теста. Вопросы — обычный
    текст: книга ответов фейковой модели зовёт по ним orders и отвечает
    после итога. Карточка приложения — блок основной ленты под контейнером
    хода; внутри неё iframe страницы-прокси песочницы и в нём iframe самого
    приложения. Ошибки страницы чата копятся с открытия: их не должно быть."""

    CARD: ClassVar[str] = "[data-mcp-app]"
    SHOWN: ClassVar[str] = "result: 3 orders are shown by their app"

    def __init__(self, chat: ChatPage, llm_port: int) -> None:
        self.chat = chat
        self.errors: list[str] = []
        chat.page.on("pageerror", self._failed)
        ReplyBookClient(StandUrl.of(llm_port)).register(OrdersBook().replies())

    def _failed(self, error: Error) -> None:
        self.errors.append(str(error))

    @property
    def page(self) -> Page:
        return self.chat.page

    def ask(self) -> Locator:
        """Ход с одним вызовом orders: карточка его приложения, дорисованного
        с аргументами и итогом."""
        self.chat.ask(OrdersBook.QUESTION)
        self.chat.await_idle(timeout_sec=120.0)
        self.page.get_by_text(OrdersBook.ANSWER).last.wait_for(timeout=60_000)
        card = self.cards().last
        self.drawn(card, "3 of 3 orders")
        return card

    def cards(self) -> Locator:
        return self.page.locator(self.CARD)

    def outer(self, card: Locator) -> Locator:
        """iframe страницы-прокси песочницы в карточке."""
        return card.locator("iframe").first

    def inner(self, card: Locator) -> FrameLocator:
        """iframe приложения в странице-прокси."""
        return card.frame_locator("iframe").first.frame_locator("iframe")

    def drawn(self, card: Locator, count: str) -> FrameLocator:
        """Приложение карточки получило строки (count) и итог вызова."""
        inner = self.inner(card)
        inner.locator("#count").get_by_text(count).wait_for(timeout=60_000)
        inner.locator("#result").get_by_text("result:").wait_for(timeout=60_000)
        return inner

    def box(self, locator: Locator) -> FloatRect:
        box = locator.bounding_box()
        if box is None:
            raise AssertionError(f"{locator} is laid out on the page")

        return box

    def check_clean(self) -> None:
        if self.errors:
            raise AssertionError(f"the chat page raised errors: {self.errors}")


class TestMcpToolInTheFeed:
    def test_text_tool_is_drawn_as_a_step(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(mcp_stand, "")

        step = FeedCalls(chat).ask("mcp__standard__add", {"a": 2, "b": 3})

        if "add" not in str(step.get(StepField.NAME.value)):
            raise AssertionError(f"the step is named after the mcp tool: {step}")
        if "5" not in str(step.get(StepField.OUTPUT.value)):
            raise AssertionError(f"the step shows the server's answer: {step}")

    def test_image_of_a_tool_is_a_file_of_the_thread(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        """Картинка чужого сервера ложится файлом в хранилище треда и
        рисуется по своему типу карточкой вьювера картинок у ответа хода;
        байтов блока в ленте нет, тело файла отдаёт маршрут канваса с Range."""
        chat = open_chat(mcp_stand, "")

        step = FeedCalls(chat).ask("mcp__standard__picture", {})

        cards: list[dict[str, Any]] = []
        for frame in chat.log.of_event(ChatEvent.ELEMENT):
            if not isinstance(frame.payload, dict):
                continue

            props = frame.payload.get("props")
            if not isinstance(props, dict):
                continue

            if str(props.get("path")).endswith(".png"):
                cards.append(props)

        if len(cards) != 1:
            raise AssertionError(
                f"the feed gets one image card: {cards}\n{chat.log.describe()}"
            )

        encoded = base64.b64encode(PNG).decode("ascii")
        if encoded in str(step.get(StepField.OUTPUT.value)):
            raise AssertionError(f"the bytes of the image stay out of the step: {step}")

        name = PurePosixPath(str(cards[0].get("path"))).name
        thread_id = str(step.get(StepField.THREAD_ID.value))
        query = urlencode({"mime": str(cards[0].get("mime"))})
        url = f"{chat.base_url}/canvas/{thread_id}/upload/{name}?{query}"
        reply = chat.page.request.get(url, headers={"Range": "bytes=0-7"})
        if reply.status != 206:
            raise AssertionError(
                f"GET {url} with Range: expected 206, got {reply.status}"
            )

        if reply.body() != PNG[:8]:
            raise AssertionError(f"GET {url}: the file is the image: {reply.body()!r}")

    def test_blocks_of_a_tool_are_drawn_by_their_type(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        """Ответ из блоков разных видов: текст человеку, ресурс text/plain
        блоком кода, ссылка на файл вне workspace — пояснением; текст только
        для модели в ленту не попадает."""
        chat = open_chat(mcp_stand, "")

        step = FeedCalls(chat).ask("mcp__standard__blocks", {"empty": False})

        output = str(step.get(StepField.OUTPUT.value))
        expected = (
            "for the user\n\nfor the model\n\n"
            "/report.csv is not shown: file is outside the thread attachments dir"
        )
        if not output.startswith(expected):
            raise AssertionError(f"the blocks are drawn in order: {output!r}")

        if "```notes.txt\nnotes\n```" not in output:
            raise AssertionError(f"the text resource is a code block: {output!r}")

    def test_json_of_a_tool_is_a_tree_under_the_step(
        self, mcp_stand: StandProcess, open_chat: OpenChat, app_texts: UiText
    ) -> None:
        """Look-тест дерева JSON: корень раскрыт, вложенное свёрнуто и
        раскрывается кликом; строка с разметкой видна текстом и ничего не
        исполняет."""
        chat = open_chat(mcp_stand, "")
        page = chat.page

        FeedCalls(chat).ask("mcp__standard__blocks", {"empty": False})
        step = chat.expand_last_tool()

        tree = step.locator("[data-json-tree]").last
        tree.wait_for(state="visible", timeout=30_000)
        rows = tree.locator("[data-json-node] [data-json-node]").first

        one_item = app_texts.text("aid.json.item_one", count=1)
        folded = tree.inner_text()
        if '"total": 1' not in folded:
            raise AssertionError(f"the root is expanded: {folded!r}")
        if f"{one_item} ]" not in folded:
            raise AssertionError(f"the nested array is folded: {folded!r}")
        if "<script>" in folded:
            raise AssertionError(f"the nested string is folded away: {folded!r}")

        rows.locator("summary").first.click()
        row = rows.locator("[data-json-node]").first
        row.locator("summary").first.click()

        script = '"<script>window.__boba_pwned=1</script>"'
        tree.get_by_text(script).wait_for(state="visible", timeout=10_000)
        unfolded = tree.inner_text()
        if script not in unfolded:
            raise AssertionError(f"the string is shown as text: {unfolded!r}")
        if tree.locator("script").count():
            raise AssertionError("the string became markup in the page")
        if page.evaluate("() => window.__boba_pwned") is not None:
            raise AssertionError("the string ran as a script")

        rows.locator("summary").first.click()
        folded_back = tree.get_by_text(re.compile(re.escape(one_item)))
        folded_back.wait_for(state="visible", timeout=10_000)
        if f"{one_item} ]" not in tree.inner_text():
            raise AssertionError(f"the array folds back: {tree.inner_text()!r}")


class TestOwnServerInTheFeed:
    def test_tool_of_boba_mcp_is_drawn_with_its_result(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(mcp_stand, "")

        step = FeedCalls(chat).ask("mcp__boba__fake_echo", {"text": "hi", "repeat": 2})

        if "fake_echo" not in str(step.get(StepField.NAME.value)):
            raise AssertionError(f"the step is named after the tool: {step}")
        if "hi hi|t0ken" not in str(step.get(StepField.OUTPUT.value)):
            raise AssertionError(f"the step shows the result of the service: {step}")

    def test_body_of_boba_mcp_runs_as_the_chat_user(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        """Сервис узнаёт пользователя чата: логин и роли, с которыми тот
        вошёл в чат, приходят при входе proxy; тело исполняется от его имени."""
        chat = open_chat(mcp_stand, "")

        step = FeedCalls(chat).ask("mcp__boba__fake_whoami", {})

        output = str(step.get(StepField.OUTPUT.value))
        if not output.startswith(f"{CHAT_LOGIN}|{CHAT_ROLE}|"):
            raise AssertionError(
                f"the body runs as the user of the chat with his roles: {step}"
            )

    def test_body_of_boba_mcp_runs_in_the_scope_of_the_thread(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        """Область вызова — тред чата: ретранслятор ставит его в параметр
        thread_id каждого вызова, поэтому инструменты видят workspace треда —
        файлы, загруженные пользователем, и то, что открывает панель канваса."""
        chat = open_chat(mcp_stand, "")

        step = FeedCalls(chat).ask("mcp__boba__fake_scope", {})

        output = str(step.get(StepField.OUTPUT.value)).strip()
        thread_id = str(step.get(StepField.THREAD_ID.value))
        if output != thread_id:
            raise AssertionError(
                f"the body runs in the scope of the thread {thread_id}: {step}"
            )

    def test_failed_body_of_boba_mcp_is_a_failed_step(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(mcp_stand, "")

        step = FeedCalls(chat).ask(
            "mcp__boba__fake_echo", {"text": "boom", "repeat": 1}
        )

        if "fake backend is down" not in str(step.get(StepField.OUTPUT.value)):
            raise AssertionError(f"the step shows the cause of the failure: {step}")
        if not step.get(StepField.IS_ERROR.value):
            raise AssertionError(f"the step is marked as failed: {step}")

    def test_arguments_look_the_same_after_the_page_is_reopened(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        """Аргументы вызова после повторного захода на страницу выглядят как в
        живой ленте: запрос блоком кода sql, а не сырым json. Шаги треда
        страница получает воспроизведением истории агента при возврате к
        треду — тем же шагом с тем же id."""
        chat = open_chat(mcp_stand, "")

        live = FeedCalls(chat).ask(
            "mcp__boba__fake_query", {"sql": "select 42", "limit": 3}
        )

        shown = str(live.get(StepField.INPUT.value))
        if not shown.startswith("```sql\nselect 42\n```"):
            raise AssertionError(f"the live step shows the query as sql: {shown!r}")

        thread_id = str(live.get(StepField.THREAD_ID.value))
        chat.log.clear()
        chat.page.goto(
            f"{chat.base_url}/thread/{thread_id}", wait_until="domcontentloaded"
        )
        chat.page.wait_for_selector(Selector.INPUT.value, timeout=60_000)
        chat.await_step(StepKind.ASSISTANT.value)

        replayed: dict[str, str] = {}
        for step in chat.log.steps_of_type(StepKind.TOOL.value):
            replayed[str(step.get(StepField.ID.value))] = str(
                step.get(StepField.INPUT.value)
            )

        if replayed != {str(live.get(StepField.ID.value)): shown}:
            raise AssertionError(
                f"the reopened page shows the arguments as the live feed: "
                f"{replayed!r} != {shown!r}\n{chat.log.describe()}"
            )

    def test_journal_of_a_boba_mcp_call_opens_in_the_panel(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        """Журнал лежит у сервиса: кнопка шага открывает его в панели, текст
        панель читает окном через клиента MCP."""
        chat = open_chat(mcp_stand, "")

        FeedCalls(chat).ask("mcp__boba__fake_echo", {"text": "journal", "repeat": 1})

        tool = chat.expand_last_tool()
        tool.locator("[data-canvas-stream]").click()

        side = chat.page.locator("#side-view-content")
        side.get_by_text("echo progress: journal").first.wait_for(timeout=30_000)


class TestToolApps:
    """Приложение MCP Apps в основной ленте: карточка под контейнером хода,
    шаг вызова — только факт вызова. Кнопки приложения зовут инструменты
    своего сервера только с "app" в visibility, ui/message доходит до агента,
    контекст приложения уходит модели со следующим вопросом; страница и куки
    чата скрипту приложения недоступны; после перезагрузки треда карточка та
    же. Сервер без секции apps показывает итог блоками."""

    @staticmethod
    def _llm_requests(page: Page, llm_port: int, marker: str) -> list[str]:
        """Сообщения запросов фейковой модели с marker, JSON-текстом."""
        reply = page.request.get(StandUrl.of(llm_port, "/requests"))
        found: list[str] = []
        for payload in reply.json()["requests"]:
            messages = json.dumps(payload.get("messages"))
            if marker in messages:
                found.append(messages)

        return found

    def test_card_stands_in_the_feed_and_the_step_is_the_call(
        self, mcp_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        """Карточка видна без раскрытия хода: блок основной ленты между
        контейнером и ответом; раскрытый шаг показывает аргументы и итог, но
        не приложение."""
        orders = OrdersApp(open_chat(mcp_stand, ""), llm_port)
        card = orders.ask()

        message = card.locator("xpath=ancestor::*[@data-step-type][1]")
        if message.get_attribute("data-step-type") != StepKind.ASSISTANT.value:
            raise AssertionError("the card is a message of the main feed")

        order = orders.page.evaluate(
            """(card) => {
              const blocks = [...document.querySelectorAll('[data-step-type]')]
                .filter((node) => !node.parentElement.closest('[data-step-type]'));
              return blocks.map((node) => {
                if (node.contains(card)) return 'card';
                return node.getAttribute('data-step-type');
              });
            }""",
            card.element_handle(),
        )
        turn = order[order.index(StepKind.USER.value) :]
        if turn[:4] != [
            StepKind.USER.value,
            StepKind.RUN.value,
            "card",
            "assistant_message",
        ]:
            raise AssertionError(f"question, container, card, answer: {order}")

        tool = orders.chat.expand_last_tool()
        shown = tool.inner_text()
        if "7\tAcme\tnew\t120.5" not in shown:
            raise AssertionError(f"the step shows the rows of the call: {shown!r}")

        if "3 orders are shown by their app" not in shown:
            raise AssertionError(f"the step shows the result: {shown!r}")

        if tool.locator("iframe").count():
            raise AssertionError("the step draws no app")

        orders.check_clean()

    def test_two_calls_of_a_turn_are_two_cards_in_order(
        self, mcp_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        orders = OrdersApp(open_chat(mcp_stand, ""), llm_port)
        orders.chat.ask(OrdersBook.TWO_STORES)
        orders.chat.await_idle(timeout_sec=120.0)
        orders.page.get_by_text(OrdersBook.TWO_ANSWER).last.wait_for(timeout=60_000)

        cards = orders.cards()
        if cards.count() != 2:
            raise AssertionError(f"two calls give two cards: {cards.count()}")

        orders.drawn(cards.nth(0), "2 of 2 orders")
        orders.drawn(cards.nth(1), "1 of 1 orders")
        orders.check_clean()

    def test_app_calls_the_tools_of_its_server_for_the_app(
        self, mcp_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        orders = OrdersApp(open_chat(mcp_stand, ""), llm_port)
        inner = orders.inner(orders.ask())
        answer = inner.locator("#answer")

        inner.locator("#result").get_by_text(OrdersApp.SHOWN).wait_for()
        inner.locator("#refresh").click()
        answer.get_by_text("answer: orders refreshed").wait_for(timeout=30_000)
        inner.locator("#archive").click()
        answer.get_by_text("error:").wait_for(timeout=30_000)

        refused = answer.inner_text()
        if "archive_orders" not in refused:
            raise AssertionError(f"the app is refused the tool of the model: {refused}")

        orders.check_clean()

    def test_app_sees_neither_the_cookies_nor_the_page_of_the_chat(
        self, mcp_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        orders = OrdersApp(open_chat(mcp_stand, ""), llm_port)
        probe = orders.inner(orders.ask()).locator("#probe").inner_text()

        if probe != "cookies: none; chat page: hidden":
            raise AssertionError(f"the app script is cut off from the chat: {probe}")

    def test_message_and_context_of_the_app_reach_the_agent(
        self, mcp_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        """ui/message — вопрос агенту от пользователя; ui/update-model-context
        модель получает со следующим вопросом, а лента вопроса его не
        показывает. Приложение не перезагружается, пока идёт и кончается
        новый ход: счётчик в нём остаётся."""
        orders = OrdersApp(open_chat(mcp_stand, ""), llm_port)
        inner = orders.inner(orders.ask())

        inner.locator("#refresh").click()
        inner.locator("#refreshed").get_by_text("refreshed: 1").wait_for()
        inner.locator("#context").click()
        inner.locator("#answer").get_by_text("answer:").wait_for(timeout=30_000)
        # вопрос приложения ждёт свой ход: в журнале не остаётся конца хода orders
        orders.chat.log.clear()
        inner.locator("#ask").click()
        orders.page.get_by_text(OrdersBook.ASKED).last.wait_for(timeout=60_000)
        orders.chat.await_idle(timeout_sec=120.0)

        asked = self._llm_requests(orders.page, llm_port, OrdersBook.ASKED)
        if not asked:
            raise AssertionError("ui/message of the app reaches the model")

        answered = orders.page.get_by_text(OrdersBook.ASKED_ANSWER)
        if not answered.count():
            last = json.loads(asked[-1])[-1]
            raise AssertionError(f"the book answers the question: {last!r}")

        if OrdersBook.CONTEXT not in asked[-1]:
            raise AssertionError(f"the context goes with the question: {asked[-1]}")

        if '\\"orders\\": 3' not in asked[-1]:
            raise AssertionError(f"the structured context goes as json: {asked[-1]}")

        questions = [
            str(step.get(StepField.OUTPUT.value))
            for step in orders.chat.log.steps_of_type(StepKind.USER.value)
        ]
        if any("the user sees" in question for question in questions):
            raise AssertionError(
                f"the context is not a part of the question: {questions}"
            )

        kept = inner.locator("#refreshed").inner_text()
        if kept != "refreshed: 1":
            raise AssertionError(f"the app keeps its state over a new turn: {kept}")

        orders.check_clean()

    def test_card_comes_back_with_the_thread(
        self, mcp_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        """Тред открыт заново: карточка рисуется тем же кодом по истории
        агента, приложение получает сохранённые аргументы и итог, а сервер
        второй раз не зовётся."""
        orders = OrdersApp(open_chat(mcp_stand, ""), llm_port)
        live = orders.ask()
        call_id = str(live.get_attribute("data-mcp-app"))

        step = orders.chat.log.last_step(StepKind.TOOL.value)
        if step is None:
            raise AssertionError(f"no tool step\n{orders.chat.log.describe()}")

        thread_id = str(step.get(StepField.THREAD_ID.value))
        orders.page.goto(
            f"{orders.chat.base_url}/thread/{thread_id}", wait_until="domcontentloaded"
        )
        orders.page.wait_for_selector(Selector.INPUT.value, timeout=60_000)

        orders.page.get_by_text(OrdersBook.ANSWER).last.wait_for(timeout=60_000)
        card = orders.page.locator(f'[data-mcp-app="{call_id}"]')
        card.wait_for(timeout=60_000)
        inner = orders.drawn(card, "3 of 3 orders")
        inner.locator("#result").get_by_text(OrdersApp.SHOWN).wait_for()
        orders.check_clean()

    def test_server_without_apps_shows_the_blocks(
        self, mcp_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        orders = OrdersApp(open_chat(mcp_stand, ""), llm_port)
        orders.chat.ask(OrdersBook.PLAIN)
        orders.chat.await_idle(timeout_sec=120.0)
        orders.page.get_by_text(OrdersBook.PLAIN_ANSWER).last.wait_for(timeout=60_000)

        tool = orders.chat.expand_last_tool()
        shown = tool.inner_text()
        if "#7 Acme new 120.50" not in shown:
            raise AssertionError(f"the server answers with blocks: {shown!r}")

        if orders.cards().count():
            raise AssertionError("a server without apps draws no card")


class TestToolAppLook:
    """Вид карточки приложения: рамка с края текста ответа до края пузыря
    вопроса, зазоры ленты 24px, шапка 44px, отступ тела 8px; внешний iframe
    на источнике песочницы с разрешениями стандарта, внутренний — без
    allow-same-origin и с CSP; высота по size-changed приложения; тема,
    переменные стилей и шрифт чата у приложения; режимы «на весь экран» и
    «свернуть»; узкое окно без горизонтальной прокрутки."""

    EDGE: ClassVar[float] = 1.0
    """Допуск выравнивания краёв, px: дробные координаты колонки."""

    GAP: ClassVar[float] = 24.0
    HEAD: ClassVar[float] = 44.0
    PAD: ClassVar[float] = 8.0

    def test_app_frames_follow_the_standard(
        self, mcp_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        orders = OrdersApp(open_chat(mcp_stand, ""), llm_port)
        card = orders.ask()
        outer = orders.outer(card)
        proxy = card.frame_locator("iframe").first.locator("iframe").first

        src = str(outer.get_attribute("src"))
        if not src.startswith("http://localhost:"):
            raise AssertionError(f"the sandbox is on the origin of the server: {src}")

        if (
            outer.get_attribute("sandbox")
            != "allow-scripts allow-same-origin allow-forms"
        ):
            raise AssertionError(f"the sandbox frame: {outer.get_attribute('sandbox')}")

        if proxy.get_attribute("sandbox") != "allow-scripts allow-forms":
            raise AssertionError(f"the app frame: {proxy.get_attribute('sandbox')}")

        srcdoc = str(proxy.get_attribute("srcdoc"))
        if "default-src 'none'" not in srcdoc:
            raise AssertionError(f"the app runs under its CSP: {srcdoc[:300]}")

        if "https://cdn.example.com" not in srcdoc:
            raise AssertionError(
                f"the CSP takes the domains of the resource: {srcdoc[:300]}"
            )

        font_src = re.search(r"font-src ([^;]*)", srcdoc)
        sandbox = urlsplit(src)
        if font_src is None or f"http://localhost:{sandbox.port}" not in font_src.group(
            1
        ):
            raise AssertionError(
                f"the app takes fonts from the sandbox host: {srcdoc[:400]}"
            )

        orders.check_clean()

    def test_card_is_aligned_with_the_feed(
        self, mcp_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        orders = OrdersApp(open_chat(mcp_stand, ""), llm_port)
        card = orders.ask()
        page = orders.page
        frame = orders.box(card)
        head = orders.box(card.locator("> div").first)
        app = orders.box(orders.outer(card))
        answer = page.locator('[data-step-type="assistant_message"]').filter(
            has_text=OrdersBook.ANSWER
        )
        text = orders.box(answer.last.locator(".message-content").first)
        bubble = orders.box(
            page.locator('[data-step-type="user_message"] .rounded-3xl').last
        )
        container = orders.box(page.locator('[data-step-type="run"] .ai-message').last)
        reply = orders.box(answer.last.locator(".ai-message").first)

        edges = {
            "left to the answer text": frame["x"] - text["x"],
            "right to the question bubble": (
                frame["x"] + frame["width"] - bubble["x"] - bubble["width"]
            ),
        }
        for name, shift in edges.items():
            if abs(shift) > self.EDGE:
                raise AssertionError(f"the card edge {name}: off by {shift}px")

        gaps = {
            "container to card": frame["y"] - container["y"] - container["height"],
            "card to answer": reply["y"] - frame["y"] - frame["height"],
        }
        for name, gap in gaps.items():
            if abs(gap - self.GAP) > self.EDGE:
                raise AssertionError(f"the gap {name} is {self.GAP}px: {gap}")

        if abs(head["height"] - self.HEAD) > self.EDGE:
            raise AssertionError(f"the head is {self.HEAD}px high: {head}")

        pads = {
            "left": app["x"] - frame["x"] - 1,
            "right": frame["x"] + frame["width"] - 1 - app["x"] - app["width"],
            "bottom": frame["y"] + frame["height"] - 1 - app["y"] - app["height"],
        }
        for name, pad in pads.items():
            if abs(pad - self.PAD) > self.EDGE:
                raise AssertionError(
                    f"the body pads the app by {self.PAD}px {name}: {pad}"
                )

        orders.check_clean()

    def test_frame_takes_the_height_of_the_app(
        self, mcp_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        """Высота iframe — высота документа приложения, прокрутки внутри
        нет; приложение выросло после фильтра — рамка выросла за ним."""
        orders = OrdersApp(open_chat(mcp_stand, ""), llm_port)
        card = orders.ask()
        inner = orders.inner(card)
        outer = orders.outer(card)

        inner.locator("#status").select_option("paid")
        inner.locator("#count").get_by_text("1 of 3 orders").wait_for()
        narrow = self._settled(outer, inner)
        inner.locator("#status").select_option("")
        inner.locator("#count").get_by_text("3 of 3 orders").wait_for()
        full = self._settled(outer, inner)

        if full <= narrow:
            raise AssertionError(f"the frame grows with the app: {narrow} -> {full}")

        orders.check_clean()

    @staticmethod
    def _settled(outer: Locator, inner: FrameLocator) -> float:
        """Высота iframe, когда она сравнялась с документом приложения и
        прокрутки в нём нет."""
        measure = (
            "() => [document.body.scrollHeight, "
            "document.documentElement.scrollHeight, window.innerHeight]"
        )
        body = inner.locator("body")
        for _ in range(40):
            document, scrolled, shown = body.evaluate(measure)
            box = outer.bounding_box()
            if box is not None and box["height"] == document and scrolled <= shown:
                return box["height"]

            body.page.wait_for_timeout(100)

        raise AssertionError(
            f"the frame takes the height of the app: {outer.bounding_box()} vs "
            f"{body.evaluate(measure)}"
        )

    def test_narrow_window_scrolls_no_page_sideways(
        self, mcp_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        orders = OrdersApp(open_chat(mcp_stand, ""), llm_port)
        card = orders.ask()
        orders.page.set_viewport_size({"width": 420, "height": 800})
        orders.page.wait_for_timeout(500)

        widths = orders.page.evaluate(
            "() => [document.documentElement.scrollWidth, "
            "document.documentElement.clientWidth]"
        )
        if widths[0] > widths[1]:
            raise AssertionError(f"no horizontal scroll in a narrow window: {widths}")

        frame = orders.box(card)
        app = orders.box(orders.outer(card))
        if app["width"] > frame["width"] - 2 * self.PAD:
            raise AssertionError(f"the app is no wider than its card: {app} in {frame}")

        orders.check_clean()

    def test_app_takes_the_theme_styles_and_font_of_the_chat(
        self, mcp_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        """Переменные стилей приложения — токены чата текущей темы; смена
        темы чата доходит без перезагрузки приложения; шрифт чата приложение
        берёт с хоста песочницы."""
        orders = OrdersApp(open_chat(mcp_stand, ""), llm_port)
        inner = orders.inner(orders.ask())
        inner.locator("#refresh").click()
        inner.locator("#refreshed").get_by_text("refreshed: 1").wait_for()

        for dark in (False, True):
            orders.page.evaluate(
                "(dark) => document.documentElement.classList.toggle('dark', dark)",
                dark,
            )
            theme = "light"
            if dark:
                theme = "dark"

            inner.locator("#host").get_by_text(f"theme: {theme}").wait_for()
            seen = inner.locator("body").evaluate(ThemeProbe.APP)
            wanted = orders.page.evaluate(ThemeProbe.CHAT)
            if seen != wanted:
                raise AssertionError(
                    f"{theme}: the app takes {seen}, the chat has {wanted}"
                )

        kept = inner.locator("#refreshed").inner_text()
        if kept != "refreshed: 1":
            raise AssertionError(f"the theme changes without reloading the app: {kept}")

        loaded = inner.locator("body").evaluate(ThemeProbe.FONT)
        if not loaded:
            raise AssertionError("the app loaded the Inter font of the chat")

        orders.check_clean()

    def test_full_screen_by_the_button_and_by_the_app(
        self,
        mcp_stand: StandProcess,
        open_chat: OpenChat,
        llm_port: int,
        app_texts: UiText,
    ) -> None:
        """Кнопка шапки разворачивает карточку на окно, приложение получает
        displayMode и высоту контейнера; Esc возвращает карточку. Запрос
        приложения fullscreen выполняется, pip получает текущий режим."""
        orders = OrdersApp(open_chat(mcp_stand, ""), llm_port)
        card = orders.ask()
        inner = orders.inner(card)
        expand = card.locator('[data-app-action="fullscreen"]')

        if expand.get_attribute("aria-label") != app_texts.text("aid.app.fullscreen"):
            raise AssertionError(
                f"the button is labelled: {expand.get_attribute('aria-label')}"
            )

        expand.click()
        inner.locator("#host").get_by_text(
            re.compile(r"mode: fullscreen; width: \d+; height: \d+")
        ).wait_for()
        box = orders.box(card)
        if (box["x"], box["y"], box["width"], box["height"]) != (0, 0, 1280, 900):
            raise AssertionError(f"the card covers the window: {box}")

        exit_button = card.locator('[data-app-action="exit"]')
        if exit_button.get_attribute("aria-label") != app_texts.text("aid.app.exit"):
            raise AssertionError("the exit button is labelled from the translations")

        orders.page.keyboard.press("Escape")
        inner.locator("#host").get_by_text("mode: inline").wait_for()
        if card.get_attribute("data-display-mode") != "inline":
            raise AssertionError("Esc brings the card back to the feed")

        inner.locator("#fullscreen").click()
        inner.locator("#answer").get_by_text("mode: fullscreen").wait_for()
        if card.get_attribute("data-display-mode") != "fullscreen":
            raise AssertionError("the request of the app opens the full screen")

        kept = inner.locator("body").evaluate(
            "() => requestMode('pip').then((result) => result && result.mode)"
        )
        if kept != "fullscreen":
            raise AssertionError(f"pip is refused with the current mode: {kept}")

        inner.locator("#fullscreen").click()
        inner.locator("#answer").get_by_text("mode: inline").wait_for()
        orders.check_clean()

    def test_collapsed_card_keeps_the_app(
        self,
        mcp_stand: StandProcess,
        open_chat: OpenChat,
        llm_port: int,
        app_texts: UiText,
    ) -> None:
        orders = OrdersApp(open_chat(mcp_stand, ""), llm_port)
        card = orders.ask()
        inner = orders.inner(card)
        inner.locator("#refresh").click()
        inner.locator("#refreshed").get_by_text("refreshed: 1").wait_for()

        collapse = card.locator('[data-app-action="collapse"]')
        if collapse.get_attribute("aria-label") != app_texts.text("aid.app.collapse"):
            raise AssertionError(
                "the collapse button is labelled from the translations"
            )

        collapse.click()
        folded = orders.box(card)
        if abs(folded["height"] - self.HEAD - 2) > self.EDGE:
            raise AssertionError(f"the collapsed card is its head: {folded}")

        card.locator('[data-app-action="expand"]').click()
        kept = inner.locator("#refreshed").inner_text()
        if kept != "refreshed: 1":
            raise AssertionError(f"the app keeps its state: {kept}")

        orders.check_clean()


class ThemeProbe(StrEnum):
    """Скрипты сверки темы: переменные стилей в документе приложения против
    токенов чата и загруженный шрифт Inter."""

    APP = """() => {
      const probe = document.createElement("div");
      probe.style.color = "var(--color-background-primary)";
      document.body.appendChild(probe);
      const color = getComputedStyle(probe).color;
      probe.remove();
      const style = getComputedStyle(document.documentElement);
      return [color, style.getPropertyValue("--font-sans").trim()];
    }"""
    CHAT = """() => {
      const probe = document.createElement("div");
      probe.style.color = "hsl(var(--background))";
      document.body.appendChild(probe);
      const color = getComputedStyle(probe).color;
      probe.remove();
      const style = getComputedStyle(document.documentElement);
      return [color, style.getPropertyValue("--font-sans").trim()];
    }"""
    FONT = """async () => {
      await document.fonts.ready;
      const loaded = [...document.fonts].filter((face) =>
        face.family.replace(/['"]/g, "") === "Inter" && face.status === "loaded");
      return loaded.length > 0;
    }"""


class TestSandboxPage:
    """Страница-прокси песочницы сама по себе: CSP приложения встаёт первым
    узлом документа при любой разметке, домены, под которые подпадает
    источник чата, в CSP не попадают. Страница открыта верхним окном и
    шлёт себе sandbox-resource-ready — источник чата здесь её же источник."""

    PROBE: str = (
        "<script>fetch({target!r}, {{mode: 'no-cors'}})"
        ".then(() => {{ window.reached = 'reached'; }})"
        ".catch(() => {{ window.reached = 'blocked'; }});</script>"
    )

    @staticmethod
    def _loaded(
        browser: Browser, stand: StandProcess, html: str, csp: dict[str, Any] | None
    ) -> Page:
        sandbox = stand.config.mcp_servers["apps"]["apps"]["sandbox_url"]
        page = browser.new_page()
        page.goto(str(sandbox))
        page.evaluate(
            "([html, csp]) => window.postMessage({jsonrpc: '2.0', method: "
            "'ui/notifications/sandbox-resource-ready', params: {html, csp}}, '*')",
            [html, csp],
        )
        page.locator("iframe").wait_for()

        return page

    def _reached(
        self,
        browser: Browser,
        stand: StandProcess,
        html: str,
        csp: dict[str, Any] | None,
    ) -> str:
        """Исход запроса скрипта приложения: reached или blocked его CSP."""
        page = self._loaded(browser, stand, html, csp)
        try:
            app = page.frames[-1]
            app.wait_for_function("window.reached !== undefined", timeout=10_000)
            return str(app.evaluate("window.reached"))
        finally:
            page.close()

    @pytest.mark.parametrize(
        "markup",
        [
            "<!--<head>--><html><body>{probe}</body></html>",
            "<html><header>board</header>{probe}</html>",
            "<!DOCTYPE html>{probe}<head></head><body></body>",
        ],
        ids=["head-in-a-comment", "header-without-head", "script-before-head"],
    )
    def test_csp_of_the_app_is_the_first_node_of_any_markup(
        self, mcp_stand: StandProcess, browser: Browser, markup: str
    ) -> None:
        chat = urlsplit(mcp_stand.config.base_url)
        target = urlunsplit((chat.scheme, chat.netloc, "/", "", ""))
        html = markup.format(probe=self.PROBE.format(target=target))
        allowed = {"connectDomains": [target]}

        blocked = self._reached(browser, mcp_stand, html, None)
        reached = self._reached(browser, mcp_stand, html, allowed)

        assert (blocked, reached) == ("blocked", "reached")

    def test_domains_over_the_chat_origin_leave_the_csp(
        self, mcp_stand: StandProcess, browser: Browser
    ) -> None:
        sandbox = str(mcp_stand.config.mcp_servers["apps"]["apps"]["sandbox_url"])
        port = urlsplit(sandbox).port
        csp = {
            "connectDomains": [
                "*",
                "http://*",
                "http://localhost",
                f"http://localhost:{port}",
                "http://localhost:*",
                "https://cdn.example.com",
            ]
        }
        page = self._loaded(browser, mcp_stand, "<p>board</p>", csp)
        try:
            srcdoc = str(page.locator("iframe").get_attribute("srcdoc"))
        finally:
            page.close()

        connect = re.search(r"connect-src ([^;]*)", srcdoc)

        assert connect is not None, srcdoc
        assert connect.group(1).split() == ["'self'", "https://cdn.example.com"]


class TestServerQuestion:
    """Вопрос сервера MCP (input_required с формой): агент задаёт его чату
    формой `elicitation/create`, чат — панелью вместо поля ввода с именем
    сервера и его сообщением; ответ уходит серверу повтором вызова, запись
    ответа встаёт сообщением основной ленты после итога вызова."""

    PANEL: str = "#question-dock [data-question-panel]"
    PROMPT: str = "Загрузи заказы в хранилище"
    LOADED: str = "Строки загружены."

    def _asked(self, chat: ChatPage, llm_port: int) -> None:
        """Обычный вопрос пользователя; модель по книге ответов зовёт
        load_rows, после итога отвечает текстом."""
        call = ReplyCall(
            tool="mcp__standard__load_rows",
            arguments={"rows": 10},
            intent="Загрузить заказы",
        )
        turns = [ReplyTurn(calls=[call]), ReplyTurn(content=self.LOADED)]
        ReplyBookClient(StandUrl.of(llm_port)).register(
            [Reply(question=self.PROMPT, turns=turns)]
        )
        chat.ask(self.PROMPT)

    def _finished(self, chat: ChatPage) -> dict[str, Any]:
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            chat.page.wait_for_timeout(100)
            step = chat.log.last_step(StepKind.TOOL.value)
            if step is None:
                continue

            if str(step.get(StepField.NAME.value)).startswith(FeedCalls.RUNNING):
                continue

            return step

        raise AssertionError(f"no finished tool step\n{chat.log.describe()}")

    def test_server_question_is_answered_in_the_panel(
        self,
        mcp_stand: StandProcess,
        open_chat: OpenChat,
        stand_workdir: Path,
        llm_port: int,
    ) -> None:
        chat = open_chat(mcp_stand, "")
        self._asked(chat, llm_port)
        panel = chat.page.locator(self.PANEL)
        panel.wait_for(state="visible", timeout=60_000)

        source = panel.locator(".q-source").inner_text()
        if source != (
            "standard · load_rows needs the target table before it can start the copy."
        ):
            raise AssertionError(
                f"the panel names the server and its message: {source}"
            )

        if panel.locator(".navTab").all_inner_texts() != ["Target"]:
            raise AssertionError("the tab is the title of the field")

        labels = panel.locator(".optionLabel").all_inner_texts()
        if labels != ["sales.orders", "sales.orders_stage", "tmp.orders_copy"]:
            raise AssertionError(f"the options of a server form, no Other: {labels}")

        look = stand_workdir / "question-look"
        look.mkdir(parents=True, exist_ok=True)
        for dark, theme in ((False, "light"), (True, "dark")):
            chat.page.evaluate(
                "(dark) => document.documentElement.classList.toggle('dark', dark)",
                dark,
            )
            chat.page.wait_for_timeout(150)
            path = look / f"09-mcp-elicitation-extension-{theme}.png"
            chat.page.screenshot(path=str(path))

        chat.page.evaluate("() => document.documentElement.classList.remove('dark')")
        chat.page.wait_for_timeout(600)
        panel.locator('[role="radio"]').filter(has_text="sales.orders_stage").click()
        panel.locator("[data-submit]").click()

        step = self._finished(chat)
        output = str(step.get(StepField.OUTPUT.value))
        if "loaded 10 rows into sales.orders_stage" not in output:
            raise AssertionError(f"the server got the answer: {step}")

        # запись видна в основной ленте без раскрытия хода и шага вызова
        chat.page.get_by_text(self.LOADED).last.wait_for(timeout=60_000)
        record = chat.page.locator('[data-question-review="accept"]').last
        record.wait_for(state="visible", timeout=30_000)
        message = record.locator("xpath=ancestor::*[@data-step-type][1]")
        if message.get_attribute("data-step-type") != "assistant_message":
            raise AssertionError("the record is a message of the main feed")

        checked = record.locator('[aria-checked="true"]').inner_text()
        if "sales.orders_stage" not in checked:
            raise AssertionError(f"the record checks the answer: {checked}")

        source = record.locator(".q-source").inner_text()
        if not source.startswith("standard · "):
            raise AssertionError(f"the record names the server: {source}")

    def test_declined_server_question_reaches_the_server(
        self, mcp_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        chat = open_chat(mcp_stand, "")
        self._asked(chat, llm_port)
        panel = chat.page.locator(self.PANEL)
        panel.wait_for(state="visible", timeout=60_000)
        chat.page.wait_for_timeout(600)
        chat.page.keyboard.press("Escape")

        step = self._finished(chat)
        if "the user answered decline" not in str(step.get(StepField.OUTPUT.value)):
            raise AssertionError(f"the server got the refusal: {step}")
