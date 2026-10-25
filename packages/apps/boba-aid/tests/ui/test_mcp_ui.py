"""Инструмент MCP-сервера в живой ленте чата.

Стенд поднимается с MCP-серверами: обычным (официальный SDK, stdio),
который о boba не знает, своим boba-mcp отдельным процессом по HTTP и тем же
обычным сервером по HTTP дважды — с секцией apps (его приложения рисуются в
песочнице, которую чат отдаёт сам на своём порту того же хоста 127.0.0.1) и
без неё. Второй стенд —
с официальными примерами MCP Apps редакции 2025-11-25 из контейнера
boba-testapps, у каждого своя песочница: чат согласует с ними initialize.
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
import io
import json
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest
from acp_stand import AppsHost, OrdersBook, SandboxHost
from chat_ui import BOOT_TIMEOUT_SEC, AppReads, OpenChat
from PIL import Image, ImageChops
from playwright.sync_api import (
    Browser,
    ConsoleMessage,
    Error,
    FloatRect,
    FrameLocator,
    Locator,
    Page,
    Request,
    Route,
    expect,
)

from boba.aid.chat.ui_text import UiText
from boba.mcp_client.client import (
    HttpEndpoint,
    HttpLocation,
    McpServerConfig,
    McpServerLimits,
)
from boba.stand.apps_slot import AppServer, AppsSlot
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
        service = {"scheme": "http", "host": "127.0.0.1", "port": self.port, "path": ""}

        return {
            "limits": {"connect_timeout_sec": 30.0, "call_timeout_sec": 60.0},
            "endpoint": {
                "transport": "streamable-http",
                "service": service,
                "path": "/service",
                "auth": {
                    "auth": "proxy",
                    "issuer": service,
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
def apps_host() -> Iterator[AppsHost]:
    host = AppsHost(())
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
            "/report.csv is not shown: file is outside the scope attachments dir"
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
        """Область вызова — тред чата: сервер объявил расширение области
        вызова, и ретранслятор ставит тред в помеченный параметр scope_id
        каждого вызова, поэтому инструменты видят workspace треда — файлы,
        загруженные пользователем, и то, что открывает панель канваса."""
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

        if replayed.get(str(live.get(StepField.ID.value))) != shown:
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


@pytest.fixture
def lone_host() -> Iterator[AppsHost]:
    """Свой стандартный сервер с секцией apps: тест останавливает его сервер
    MCP, песочница остаётся."""
    host = AppsHost(())
    try:
        host.await_listening()
        yield host
    finally:
        host.stop()


@pytest.fixture
def lone_stand(
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
    lone_host: AppsHost,
) -> Iterator[StandProcess]:
    """Стенд чата с одним сервером apps на своём хосте lone_host."""
    config = StandConfig(
        workdir=stand_workdir / "chat-lone",
        app=StandApp.AID,
        app_port=StandPorts().free(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-lone",
        mcp_servers={"apps": lone_host.drawn()},
    )
    process = StandProcess(config=config, log_path=stand_workdir / "lone-app.log")
    try:
        process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
        yield process
    finally:
        process.stop()


class TestToolAppFromHistory:
    """Тред с вызовом инструмента-приложения открыт заново, когда сервера
    MCP уже нет: карточка, приложение и его кадр те же, что вживую, — они
    рисуются из источника вызова в истории агента; вкладка ресурс
    приложения не просит, а чат не спрашивает сервер."""

    SHOTS: ClassVar[int] = 20
    PAUSE_MS: ClassVar[int] = 100

    def _shot(self, card: Locator) -> bytes:
        """Кадр карточки, когда он устоялся: два снимка подряд равны."""
        previous = card.screenshot(animations="disabled", caret="hide", scale="css")
        for _ in range(self.SHOTS):
            card.page.wait_for_timeout(self.PAUSE_MS)
            current = card.screenshot(animations="disabled", caret="hide", scale="css")
            if current == previous:
                return current

            previous = current

        raise AssertionError(f"the card {card} did not settle in {self.SHOTS} shots")

    SMOOTHING: ClassVar[int] = 2
    """Разница канала, которую даёт сглаживание нативных элементов формы
    (стрелка select) в другой вкладке: это ещё тот же кадр."""

    def _changed(
        self, expected: bytes, actual: bytes
    ) -> tuple[int, int, int, int] | None:
        """Область, где кадры различаются пикселями сильнее сглаживания;
        None — кадр тот же."""
        old = Image.open(io.BytesIO(expected)).convert("RGB")
        new = Image.open(io.BytesIO(actual)).convert("RGB")
        if old.size != new.size:
            return (0, 0, new.width, new.height)

        delta = ImageChops.difference(old, new).convert("L")

        return delta.point(self._over).getbbox()

    def _over(self, value: int) -> int:
        if value > self.SMOOTHING:
            return 255

        return 0

    def test_card_comes_back_from_the_history_without_the_server(
        self,
        lone_stand: StandProcess,
        lone_host: AppsHost,
        open_chat: OpenChat,
        llm_port: int,
    ) -> None:
        orders = OrdersApp(open_chat(lone_stand, ""), llm_port)
        live = orders.ask()
        call_id = str(live.get_attribute("data-mcp-app"))
        orders.inner(live).locator("#result").get_by_text(OrdersApp.SHOWN).wait_for()
        frame = self._shot(live)
        answer = orders.chat.log.last_step(StepKind.ASSISTANT.value)
        assert answer is not None, orders.chat.log.describe()
        thread_id = str(answer.get(StepField.THREAD_ID.value))

        lone_host.stop_server()
        again = open_chat(lone_stand, "")
        reads = AppReads(again.page)
        reopened = OrdersApp(again, llm_port)
        again.open_thread(thread_id, str(answer[StepField.ID.value]))
        card = again.page.locator(f'[data-mcp-app="{call_id}"]')
        card.wait_for(timeout=60_000)
        inner = reopened.drawn(card, "3 of 3 orders")
        inner.locator("#result").get_by_text(OrdersApp.SHOWN).wait_for()

        changed = self._changed(frame, self._shot(card))
        assert changed is None, f"the replayed card differs in {changed}"
        assert reads.seen == [], reads.seen
        reopened.check_clean()


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
        sandbox = StandUrl.of(mcp_stand.config.mcp_servers["apps"]["apps"]["port"], "/")
        page = urlsplit(src)
        if urlunsplit((page.scheme, page.netloc, page.path, "", "")) != sandbox:
            raise AssertionError(f"the sandbox is on the port of the server: {src}")

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
        if font_src is None or sandbox.rstrip("/") not in font_src.group(1):
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
        sandbox = StandUrl.of(stand.config.mcp_servers["apps"]["apps"]["port"], "/")
        page = browser.new_page()
        page.goto(sandbox)
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
        port = mcp_stand.config.mcp_servers["apps"]["apps"]["port"]
        csp = {
            "connectDomains": [
                "*",
                "http://*",
                f"http://{StandUrl.HOST}",
                f"http://{StandUrl.HOST}:{port}",
                f"http://{StandUrl.HOST}:*",
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
        assert connect.group(1).split() == ["https://cdn.example.com"]


class SandboxProbe(StrEnum):
    """Пробы скрипта приложения в iframe песочницы: чтение кук, запрос и
    отправка формы. Запрос и форма отвечают директивой CSP, которая их
    остановила (none — нарушения не было за пять секунд)."""

    COOKIE = """() => {
      try {
        return "read: " + document.cookie;
      } catch (error) {
        return error.name;
      }
    }"""
    FETCH = """async (target) => {
      const violated = new Promise((resolve) => {
        document.addEventListener(
          "securitypolicyviolation",
          (event) => resolve(event.effectiveDirective),
          { once: true },
        );
        setTimeout(() => resolve("none"), 5000);
      });
      let outcome = "reached";
      try {
        await fetch(target, { credentials: "include", mode: "no-cors" });
      } catch (error) {
        outcome = error.name;
      }
      return [outcome, await violated];
    }"""
    FORM = """(target) => new Promise((resolve) => {
      document.addEventListener(
        "securitypolicyviolation",
        (event) => resolve(event.effectiveDirective),
        { once: true },
      );
      setTimeout(() => resolve("none"), 5000);
      const form = document.createElement("form");
      form.method = "post";
      form.action = target;
      document.body.appendChild(form);
      form.submit();
    })"""


class TestSandboxOrigin:
    """Песочницу приложений отдаёт сам чат на своём порту того же хоста, без
    nginx: браузер ходит в aid напрямую. Куки хоста по портам не разделены,
    поэтому изоляцию держат непрозрачный источник приложения и CSP: скрипт
    приложения не читает document.cookie, его запросы к чату и к порту
    песочницы и формы в чат останавливает CSP. Порт песочницы отдаёт только
    страницу-прокси и шрифт, встроить страницу может только чат."""

    PROBE: ClassVar[str] = "sandbox-probe=visible"
    FONTS: ClassVar[Path] = (
        Path(__file__).parents[2] / "assets" / "public" / "vendor" / "inter" / "files"
    )

    @staticmethod
    def _sandbox(stand: StandProcess) -> str:
        return StandUrl.of(stand.config.mcp_servers["apps"]["apps"]["port"], "/")

    @staticmethod
    def _chat_origin(stand: StandProcess) -> str:
        chat = urlsplit(stand.config.base_url)
        return urlunsplit((chat.scheme, chat.netloc, "", "", ""))

    def test_app_neither_reads_cookies_nor_reaches_the_chat(
        self, mcp_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        chat = open_chat(mcp_stand, "")
        name, value = self.PROBE.split("=")
        chat.page.context.add_cookies(
            [{"name": name, "value": value, "url": mcp_stand.config.base_url}]
        )
        orders = OrdersApp(chat, llm_port)
        card = orders.ask()

        handle = orders.outer(card).element_handle()
        proxy = handle.content_frame()
        if proxy is None:
            raise AssertionError("the card holds the frame of the sandbox page")

        page = urlsplit(proxy.url)
        served = urlunsplit((page.scheme, page.netloc, page.path, "", ""))
        if served != self._sandbox(mcp_stand):
            raise AssertionError(f"the chat serves the sandbox page: {proxy.url}")

        app = proxy.child_frames[0]
        shared = str(proxy.evaluate(SandboxProbe.COOKIE.value))
        read = app.evaluate(SandboxProbe.COOKIE.value)
        targets = [mcp_stand.config.base_url + "/", self._sandbox(mcp_stand)]
        fetched: list[list[str]] = []
        for target in targets:
            fetched.append(app.evaluate(SandboxProbe.FETCH.value, target))

        posted = app.evaluate(SandboxProbe.FORM.value, mcp_stand.config.base_url + "/")

        assert self.PROBE in shared, "cookies of the host reach every port of it"
        assert read == "SecurityError"
        assert fetched == [["TypeError", "connect-src"], ["TypeError", "connect-src"]]
        assert posted == "form-action"
        orders.check_clean()

    def test_sandbox_port_serves_only_its_page_and_fonts(
        self, mcp_stand: StandProcess
    ) -> None:
        prefix = mcp_stand.config.url_prefix
        faces = sorted(self.FONTS.glob("*.woff2"))
        if not faces:
            raise AssertionError(f"no Inter fonts in {self.FONTS}: make -C build web")

        with httpx.Client(base_url=self._sandbox(mcp_stand), timeout=30.0) as client:
            page = client.get("/")
            font = client.get(f"/fonts/inter/files/{faces[0].name}")
            posted = client.post("/")
            foreign: list[int] = []
            for path in (f"{prefix}/", f"{prefix}/login", f"{prefix}/auth/config"):
                foreign.append(client.get(path).status_code)

        policy = (
            f"frame-ancestors {self._chat_origin(mcp_stand)}; "
            "form-action 'none'; object-src 'none'"
        )

        assert page.status_code == HTTPStatus.OK
        assert "<title>MCP Apps sandbox</title>" in page.text
        assert page.headers["content-security-policy"] == policy
        assert "set-cookie" not in page.headers
        assert font.status_code == HTTPStatus.OK
        assert font.headers["access-control-allow-origin"] == "*"
        assert posted.status_code == HTTPStatus.METHOD_NOT_ALLOWED
        assert foreign == [HTTPStatus.NOT_FOUND] * 3

    def test_only_the_chat_frames_the_sandbox(
        self, mcp_stand: StandProcess, browser: Browser
    ) -> None:
        page = browser.new_page()
        try:
            with page.expect_console_message(
                lambda message: "frame-ancestors" in message.text, timeout=10_000
            ) as refused:
                page.set_content(f'<iframe src="{self._sandbox(mcp_stand)}"></iframe>')
        finally:
            page.close()

        assert self._chat_origin(mcp_stand) in refused.value.text


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


@pytest.fixture(scope="module")
def handshake_stand(  # noqa: PLR0913 — стенд собирается из фикстур модуля
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
    standard: McpServerConfig,
    apps_slot: AppsSlot,
) -> Iterator[StandProcess]:
    """Стенд чата с обычным сервером 2026-07-28 (standard) и своим сервером
    2025-11-25 на TypeScript SDK 1.x в контейнере boba-testapps дважды: со
    сроком вызова 120 с (legacy) и 3 с (brief)."""
    endpoint = HttpEndpoint(
        service=HttpLocation(
            scheme="http", host=apps_slot.host, port=AppServer.LEGACY.value, path=""
        ),
        path=apps_slot.PATH,
    )
    servers: dict[str, Any] = {
        "standard": standard.model_dump(mode="json", exclude_none=True),
    }
    for name, call_timeout_sec in (("legacy", 120.0), ("brief", 3.0)):
        config = McpServerConfig(
            endpoint=endpoint,
            limits=McpServerLimits(
                connect_timeout_sec=20.0, call_timeout_sec=call_timeout_sec
            ),
        )
        servers[name] = config.model_dump(mode="json", exclude_none=True)

    config = StandConfig(
        workdir=stand_workdir / "chat-handshake",
        app=StandApp.AID,
        app_port=StandPorts().free(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-handshake",
        mcp_servers=servers,
    )
    process = StandProcess(config=config, log_path=stand_workdir / "handshake.log")
    try:
        process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
        yield process
    finally:
        process.stop()


class TestHandshakeQuestion:
    """Вопрос сервера 2025-11-25 встречным запросом elicitation/create
    посреди вызова: ретранслятор держит вызов открытым и отдаёт агенту
    вопрос просьбой о вводе, агент задаёт его чату той же формой, панель —
    та же, что у сервера 2026-07-28; ответ, отказ, стоп хода и срок вызова
    доходят до сервера. Что сервер получил, тест читает его инструментом
    answers."""

    PANEL: ClassVar[str] = "#question-dock [data-question-panel]"
    MESSAGE: ClassVar[str] = (
        "load_rows needs the target table before it can start the copy."
    )
    SIGNATURE: ClassVar[str] = """(panel) => {
        const rows = [];
        for (const el of [panel, ...panel.querySelectorAll("*")]) {
            const style = getComputedStyle(el);
            const own = [...el.childNodes]
                .filter((node) => node.nodeType === Node.TEXT_NODE)
                .map((node) => node.textContent.trim())
                .join(" ");
            const box = el.getBoundingClientRect();
            rows.push([
                el.tagName, String(el.className), el.getAttribute("role"), own,
                style.color, style.backgroundColor, style.fontSize, style.fontWeight,
                style.padding, style.borderTopWidth, style.borderTopColor,
                Math.round(box.width), Math.round(box.height),
            ].join("|"));
        }
        return rows;
    }"""
    """Вид панели: каждый элемент — тег, классы, роль, свой текст, цвета,
    кегль, отступы, рамка и размер."""

    def _called(self, chat: ChatPage, tool: str, arguments: dict[str, Any]) -> None:
        """Ход, в котором модель сразу зовёт tool; ход не ждём — его держит
        вопрос."""
        request = json.dumps({"name": tool, "arguments": arguments})
        chat.ask(f"{ScenarioName.CALL.value} {request}")

    def _panel(self, chat: ChatPage) -> Locator:
        panel = chat.page.locator(self.PANEL)
        panel.wait_for(state="visible", timeout=60_000)
        chat.page.wait_for_timeout(600)

        return panel

    def _output(self, chat: ChatPage) -> str:
        """Итог последнего вызова законченного хода."""
        chat.await_idle(timeout_sec=120.0)
        step = chat.log.last_step(StepKind.TOOL.value)
        if step is None:
            raise AssertionError(f"no tool step\n{chat.log.describe()}")

        return str(step.get(StepField.OUTPUT.value))

    def _answers(self, chat: ChatPage, rows: int) -> list[dict[str, Any]]:
        """Ответы, которые сервер получил на вопросы load_rows с rows строк."""
        step = FeedCalls(chat).ask("mcp__legacy__answers", {"rows": rows})
        output = str(step.get(StepField.OUTPUT.value))
        found = re.search(r"\[.*\]", output, re.S)
        if found is None:
            raise AssertionError(f"answers gives a JSON list: {output}")

        return json.loads(found.group(0))

    def _signature(self, panel: Locator, server: str) -> list[str]:
        """Вид панели без имени сервера: его элемент — без текста и ширины."""
        rows: list[str] = []
        for row in panel.evaluate(self.SIGNATURE):
            fields = str(row).split("|")
            if fields[3] == server:
                fields[3] = "<server>"
                fields[11] = "<width>"

            rows.append("|".join(fields))

        return rows

    def test_panel_of_the_handshake_era_is_the_panel_of_the_modern_one(
        self,
        handshake_stand: StandProcess,
        open_chat: OpenChat,
        stand_workdir: Path,
    ) -> None:
        """Та же форма сервера 2026-07-28 (MRTR) и сервера 2025-11-25
        (встречный запрос) — одна и та же панель в обеих темах; ответы
        доходят до обоих серверов."""
        chat = open_chat(handshake_stand, "")
        look = stand_workdir / "question-look"
        look.mkdir(parents=True, exist_ok=True)
        seen: dict[str, dict[str, list[str]]] = {}
        calls = (
            ("standard", {"rows": 41}),
            ("legacy", {"rows": 41, "confirm": False}),
        )
        for server, arguments in calls:
            self._called(chat, f"mcp__{server}__load_rows", arguments)
            panel = self._panel(chat)
            source = panel.locator(".q-source").inner_text()
            if source != f"{server} · {self.MESSAGE}":
                raise AssertionError(
                    f"the panel names {server} and its message: {source}"
                )

            themes: dict[str, list[str]] = {}
            for dark, theme in ((False, "light"), (True, "dark")):
                chat.page.evaluate(
                    "(dark) => document.documentElement.classList.toggle('dark', dark)",
                    dark,
                )
                chat.page.wait_for_timeout(300)
                themes[theme] = self._signature(panel, server)
                panel.screenshot(path=str(look / f"09-panel-{server}-{theme}.png"))

            chat.page.evaluate(
                "() => document.documentElement.classList.remove('dark')"
            )
            chat.page.wait_for_timeout(300)
            seen[server] = themes
            panel.locator('[role="radio"]').filter(
                has_text="sales.orders_stage"
            ).click()
            panel.locator("[data-submit]").click()
            output = self._output(chat)
            if "loaded 41 rows into sales.orders_stage" not in output:
                raise AssertionError(f"{server} got the answer: {output}")

        for theme in ("light", "dark"):
            modern = seen["standard"][theme]
            handshake = seen["legacy"][theme]
            if modern != handshake:
                differ: list[str] = []
                for left, right in zip(modern, handshake, strict=False):
                    if left != right:
                        differ.append(f"{left}\n  != {right}")

                raise AssertionError(
                    f"{theme}: the panels differ ({len(modern)} and "
                    f"{len(handshake)} elements):\n" + "\n".join(differ[:10])
                )

    def test_two_questions_in_one_call_are_answered_in_the_panel(
        self, handshake_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(handshake_stand, "")
        self._called(chat, "mcp__legacy__load_rows", {"rows": 42, "confirm": True})
        panel = self._panel(chat)
        labels = panel.locator(".optionLabel").all_inner_texts()
        if labels != ["sales.orders", "sales.orders_stage", "tmp.orders_copy"]:
            raise AssertionError(f"the options of the first question: {labels}")

        panel.locator('[role="radio"]').filter(has_text="tmp.orders_copy").click()
        panel.locator("[data-submit]").click()
        panel.locator(".q-source").filter(has_text="asks to start the copy").wait_for(
            timeout=60_000
        )
        chat.page.wait_for_timeout(600)
        source = panel.locator(".q-source").inner_text()
        expected = (
            "legacy · load_rows asks to start the copy of 42 rows into tmp.orders_copy."
        )
        if source != expected:
            raise AssertionError(f"the second question: {source}")

        panel.locator('[role="radio"]').first.click()
        panel.locator("[data-submit]").click()
        output = self._output(chat)
        if "loaded 42 rows into tmp.orders_copy" not in output:
            raise AssertionError(f"the server got both answers: {output}")

        records = chat.page.locator('[data-question-review="accept"]')
        records.nth(1).wait_for(state="visible", timeout=30_000)
        answers = self._answers(chat, 42)
        if [answer["content"] for answer in answers] != [
            {"table": "tmp.orders_copy"},
            {"go": True},
        ]:
            raise AssertionError(f"the server got the answers: {answers}")

    def test_declined_question_reaches_the_server(
        self, handshake_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(handshake_stand, "")
        self._called(chat, "mcp__legacy__load_rows", {"rows": 43, "confirm": False})
        self._panel(chat)
        chat.page.keyboard.press("Escape")

        output = self._output(chat)
        if "the user answered decline" not in output:
            raise AssertionError(f"the server got the refusal: {output}")

        actions = [answer["action"] for answer in self._answers(chat, 43)]
        if actions != ["decline"]:
            raise AssertionError(f"the server got decline: {actions}")

    def test_stop_of_the_turn_cancels_the_question_on_the_server(
        self, handshake_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(handshake_stand, "")
        self._called(chat, "mcp__legacy__load_rows", {"rows": 44, "confirm": False})
        self._panel(chat)
        thread_url = f"{chat.base_url}/thread/{chat.log.thread_id()}"
        second = chat.page.context.new_page()
        try:
            second.goto(thread_url, wait_until="domcontentloaded")
            stop = second.locator("#stop-button")
            stop.wait_for(state="visible", timeout=60_000)
            stop.click()
            chat.page.locator(self.PANEL).wait_for(state="detached", timeout=30_000)
        finally:
            second.close()

        chat.await_idle(timeout_sec=120.0)
        deadline = time.monotonic() + 30.0
        actions: list[str] = []
        while time.monotonic() < deadline:
            actions = [answer["action"] for answer in self._answers(chat, 44)]
            if actions:
                break

        if actions != ["cancel"]:
            raise AssertionError(f"the server got cancel after the stop: {actions}")

    def test_answer_after_the_call_time_is_an_error_and_the_server_got_cancel(
        self, handshake_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        """Срок вызова сервера (3 с) вышел раньше ответа: сервер получил
        cancel, поздний ответ — ошибка вызова с причиной."""
        chat = open_chat(handshake_stand, "")
        self._called(chat, "mcp__brief__load_rows", {"rows": 45, "confirm": False})
        panel = self._panel(chat)
        chat.page.wait_for_timeout(4_000)
        panel.locator('[role="radio"]').filter(has_text="sales.orders").first.click()
        panel.locator("[data-submit]").click()

        output = self._output(chat)
        if "names no call the relay holds open" not in output:
            raise AssertionError(f"the late answer is an error: {output}")

        if "call_timeout_sec of [mcp.servers.brief]" not in output:
            raise AssertionError(f"the error names the setting: {output}")

        actions = [answer["action"] for answer in self._answers(chat, 45)]
        if actions != ["cancel"]:
            raise AssertionError(f"the server got cancel: {actions}")


@pytest.fixture(scope="module")
def example_stand(
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
    apps_slot: AppsSlot,
) -> Iterator[StandProcess]:
    """Стенд чата с официальными примерами MCP Apps из npm (сегменты
    клиентов и отладка) в контейнере boba-testapps, у каждого — своя
    песочница на своём источнике."""
    segments = SandboxHost()
    debug = SandboxHost()
    config = StandConfig(
        workdir=stand_workdir / "chat-examples",
        app=StandApp.AID,
        app_port=StandPorts().free(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-examples",
        mcp_servers={
            "segments": segments.drawn(apps_slot.host, AppServer.SEGMENTS.value),
            "debug": debug.drawn(apps_slot.host, AppServer.DEBUG.value),
        },
    )
    process = StandProcess(config=config, log_path=stand_workdir / "examples.log")
    try:
        process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
        yield process
    finally:
        process.stop()


class ExampleBook(StrEnum):
    """Вопросы и ответы книги фейковой модели для примеров MCP Apps."""

    SEGMENTS = "Покажи сегменты клиентов: выручка против числа сотрудников"
    SEGMENTS_ANSWER = "Крупные клиенты держатся в правом верхнем углу."
    DEBUG = "Покажи, что хост передаёт приложению"
    DEBUG_ANSWER = "Приложение отладки показывает контекст хоста."
    SLOW = "Вызови отладку с задержкой"
    SLOW_ANSWER = "Не дождался."
    MESSAGE = "Message from debug app"
    """Вопрос, который приложение отладки шлёт ui/message."""
    MESSAGE_ANSWER = "Got the message of the app."
    CONTEXT = "Current app state info"
    """Текст ui/update-model-context приложения отладки."""
    PARTIAL = "Вызови отладку с длинным вводом"
    PARTIAL_ANSWER = "Отладка с длинным вводом показана."


class ExampleApp:
    """Приложение примера MCP Apps глазами теста: вопрос книге ответов, его
    карточка в ленте и само приложение внутри страницы-прокси песочницы.
    Ошибки страницы чата копятся с открытия: их не должно быть."""

    CARD: ClassVar[str] = "[data-mcp-app]"
    WAIT_MS: ClassVar[float] = 60_000
    POLL_MS: ClassVar[float] = 200

    def __init__(
        self, chat: ChatPage, llm_port: int, extra: Sequence[Reply] = ()
    ) -> None:
        self.chat = chat
        self.errors: list[str] = []
        chat.page.on("pageerror", self._failed)
        ReplyBookClient(StandUrl.of(llm_port)).register([*self._book(), *extra])

    def _failed(self, error: Error) -> None:
        self.errors.append(str(error))

    def _book(self) -> list[Reply]:
        """Книга ответов: по вопросу — вызов инструмента примера и ответ
        после итога."""
        segments = ReplyCall(
            tool="mcp__segments__get-customer-data",
            arguments={"segment": "All"},
        )
        debug = ReplyCall(
            tool="mcp__debug__debug-tool",
            arguments={"contentType": "text", "includeStructuredContent": True},
        )
        slow = ReplyCall(
            tool="mcp__debug__debug-tool",
            arguments={"contentType": "text", "delayMs": 60000},
        )
        in_context = "\n".join((ExampleBook.MESSAGE, ExampleBook.CONTEXT))
        answered = [ReplyTurn(content=ExampleBook.MESSAGE_ANSWER)]

        return [
            Reply(
                question=ExampleBook.SEGMENTS,
                turns=[
                    ReplyTurn(calls=[segments]),
                    ReplyTurn(content=ExampleBook.SEGMENTS_ANSWER),
                ],
            ),
            Reply(
                question=ExampleBook.DEBUG,
                turns=[
                    ReplyTurn(calls=[debug]),
                    ReplyTurn(content=ExampleBook.DEBUG_ANSWER),
                ],
            ),
            Reply(
                question=ExampleBook.SLOW,
                turns=[
                    ReplyTurn(calls=[slow]),
                    ReplyTurn(content=ExampleBook.SLOW_ANSWER),
                ],
            ),
            Reply(question=ExampleBook.MESSAGE, turns=answered),
            Reply(question=in_context, turns=answered),
        ]

    @property
    def page(self) -> Page:
        return self.chat.page

    def ask(self, question: ExampleBook, answer: ExampleBook) -> Locator:
        """Ход с вызовом инструмента примера: карточка его приложения."""
        self.chat.ask(question.value)
        self.chat.await_idle(timeout_sec=120.0)
        self.page.get_by_text(answer.value).last.wait_for(timeout=self.WAIT_MS)

        return self.page.locator(self.CARD).last

    def app(self, card: Locator) -> FrameLocator:
        """Приложение в странице-прокси песочницы карточки."""
        app = card.frame_locator("iframe").first.frame_locator("iframe")
        app.locator("body").wait_for(timeout=self.WAIT_MS)

        return app

    def logged(self, app: FrameLocator, event: str) -> None:
        """Журнал приложения отладки записал событие event."""
        entry = app.locator("#event-log").get_by_text(f"{event}:").first
        entry.wait_for(timeout=self.WAIT_MS)

    def themed(self, app: FrameLocator) -> None:
        """Переменные стилей приложения сошлись с токенами чата его темы."""
        wanted = self.page.evaluate(ThemeProbe.CHAT)
        deadline = time.monotonic() + self.WAIT_MS / 1000
        seen = app.locator("body").evaluate(ThemeProbe.APP)
        while seen != wanted:
            if time.monotonic() > deadline:
                msg = f"the app takes {seen}, the chat has {wanted}"
                raise AssertionError(msg)

            self.page.wait_for_timeout(self.POLL_MS)
            seen = app.locator("body").evaluate(ThemeProbe.APP)

    def callbacks(self, app: FrameLocator) -> dict[str, int]:
        """Счётчики колбэков приложения отладки по его таблице."""
        rows = app.locator("#callback-table-body tr").all_inner_texts()
        counts: dict[str, int] = {}
        for row in rows:
            cells = row.split("\t")
            counts[cells[0].strip()] = int(cells[2].strip())

        return counts

    def check_clean(self) -> None:
        if self.errors:
            raise AssertionError(f"the chat page raised errors: {self.errors}")


class TestExampleApps:
    """Официальные примеры MCP Apps из npm (TypeScript SDK v2, редакция
    2025-11-25) в основной ленте: карточка приложения сегментов берёт тему и
    шрифт чата; приложение отладки получает ввод и итог вызова и контекст
    хоста, зовёт свой инструмент только для приложения, меняет режим
    показа, передаёт контекст модели и сообщение агенту; остановка хода
    отменяет вызов, итог которого рисует приложение."""

    def test_card_of_an_example_app_takes_the_theme_and_font_of_the_chat(
        self, example_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        example = ExampleApp(open_chat(example_stand, ""), llm_port)
        card = example.ask(ExampleBook.SEGMENTS, ExampleBook.SEGMENTS_ANSWER)
        app = example.app(card)

        message = card.locator("xpath=ancestor::*[@data-step-type][1]")
        if message.get_attribute("data-step-type") != StepKind.ASSISTANT.value:
            raise AssertionError("the card is a message of the main feed")

        for dark in (False, True):
            example.page.evaluate(
                "(dark) => document.documentElement.classList.toggle('dark', dark)",
                dark,
            )
            example.themed(app)

        if not app.locator("body").evaluate(ThemeProbe.FONT):
            raise AssertionError("the app loaded the Inter font of the chat")

        example.check_clean()

    def test_debug_app_hears_the_call_and_the_host_and_calls_its_server(
        self, example_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        example = ExampleApp(open_chat(example_stand, ""), llm_port)
        card = example.ask(ExampleBook.DEBUG, ExampleBook.DEBUG_ANSWER)
        app = example.app(card)
        example.logged(app, "ontoolresult")

        app.locator("#call-debug-refresh-btn").click()
        example.logged(app, "server-tool-result")
        app.locator("#display-fullscreen-btn").click()
        expect(card).to_have_attribute(
            "data-display-mode", "fullscreen", timeout=ExampleApp.WAIT_MS
        )
        app.locator("#display-inline-btn").click()
        expect(card).to_have_attribute(
            "data-display-mode", "inline", timeout=ExampleApp.WAIT_MS
        )
        host = app.locator("#host-info-content").inner_text()
        counts = example.callbacks(app)

        assert "boba-aid" in host, host
        assert counts["ontoolinput"] == 1, counts
        assert counts["ontoolresult"] == 1, counts
        assert counts["onhostcontextchanged"] >= 2, counts
        example.check_clean()

    def test_context_and_message_of_the_debug_app_reach_the_agent(
        self, example_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        """ui/update-model-context уходит модели со следующим вопросом —
        ui/message приложения."""
        example = ExampleApp(open_chat(example_stand, ""), llm_port)
        app = example.app(example.ask(ExampleBook.DEBUG, ExampleBook.DEBUG_ANSWER))

        app.locator("#update-context-text-btn").click()
        example.logged(app, "update-context")
        app.locator("#message-text").fill(ExampleBook.MESSAGE.value)
        app.locator("#send-message-text-btn").click()
        example.page.get_by_text(ExampleBook.MESSAGE_ANSWER.value).last.wait_for(
            timeout=ExampleApp.WAIT_MS
        )

        reply = example.page.request.get(StandUrl.of(llm_port, "/requests"))
        asked: list[str] = []
        for payload in reply.json()["requests"]:
            messages = json.dumps(payload.get("messages"), ensure_ascii=False)
            if ExampleBook.MESSAGE.value in messages:
                asked.append(messages)

        assert asked, "ui/message of the app reaches the model"
        assert ExampleBook.CONTEXT.value in asked[-1], asked[-1]
        example.check_clean()

    def test_stopped_turn_cancels_the_call_drawn_by_the_app(
        self, example_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        example = ExampleApp(open_chat(example_stand, ""), llm_port)
        example.chat.ask(ExampleBook.SLOW.value)
        card = example.page.locator(ExampleApp.CARD).last
        card.wait_for(timeout=ExampleApp.WAIT_MS)
        app = example.app(card)
        example.logged(app, "ontoolinput")

        example.page.locator("#stop-button").click()
        example.logged(app, "ontoolcancelled")
        counts = example.callbacks(app)

        assert counts["ontoolcancelled"] == 1, counts
        assert counts["ontoolresult"] == 0, counts
        example.check_clean()

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "ui/message of an app goes to the model as a plain user question "
            "without a note of its source, so the model cannot tell the text of "
            "the app of debug-tool of server debug from the words of the user"
        ),
    )
    def test_message_of_the_app_reaches_the_model_marked_with_its_source(
        self, example_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        """Текст ui/message — от недоверенного кода: в сообщении модели рядом
        с ним названы инструмент и сервер приложения."""
        example = ExampleApp(open_chat(example_stand, ""), llm_port)
        app = example.app(example.ask(ExampleBook.DEBUG, ExampleBook.DEBUG_ANSWER))

        app.locator("#message-text").fill(ExampleBook.MESSAGE.value)
        app.locator("#send-message-text-btn").click()
        example.page.get_by_text(ExampleBook.MESSAGE_ANSWER.value).last.wait_for(
            timeout=ExampleApp.WAIT_MS
        )

        reply = example.page.request.get(StandUrl.of(llm_port, "/requests"))
        carrying: list[str] = []
        for payload in reply.json()["requests"]:
            for message in payload.get("messages", []):
                rendered = json.dumps(message, ensure_ascii=False)
                if ExampleBook.MESSAGE.value in rendered:
                    carrying.append(rendered)

        assert carrying, "ui/message of the app reaches the model"
        marked = carrying[-1]
        assert "debug-tool" in marked, marked
        assert "debug" in marked.replace("debug-tool", ""), marked

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "the agent does not stream partial tool input, so the app never "
            "receives ui/notifications/tool-input-partial while the model is "
            "still writing the arguments"
        ),
    )
    def test_generative_draft_appears_before_the_result(
        self, example_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        """Модель отдаёт длинные аргументы кусками: приложение получает
        tool-input-partial раньше итога — так рисуется черновик генерации.
        Приложение отладки считает колбэки, поэтому черновик меряется им."""
        words = " ".join(f"word{index}" for index in range(80))
        slow = ReplyCall(
            tool="mcp__debug__debug-tool",
            arguments={"contentType": "text", "largeInput": words},
        )
        partial = Reply(
            question=ExampleBook.PARTIAL.value,
            turns=[
                ReplyTurn(calls=[slow]),
                ReplyTurn(content=ExampleBook.PARTIAL_ANSWER.value),
            ],
            token_delay_sec=0.05,
            stream_arguments=True,
        )
        example = ExampleApp(open_chat(example_stand, ""), llm_port, extra=[partial])
        card = example.ask(ExampleBook.PARTIAL, ExampleBook.PARTIAL_ANSWER)
        app = example.app(card)
        example.logged(app, "ontoolresult")

        counts = example.callbacks(app)
        log = "\n".join(app.locator("#event-log").all_inner_texts())

        assert counts["ontoolinputpartial"] >= 1, counts
        assert log.index("ontoolinputpartial:") < log.index("ontoolresult:"), log
        example.check_clean()


@dataclass(frozen=True)
class AppCase:
    """Ход с приложением сервера стенда: сервер в конфиге чата, вопрос книги
    фейковой модели, вызов инструмента без подписи intent (чужие серверы
    такого поля в схеме не имеют) и ответ после итога; stats_url —
    счётчик вызовов сервера (пусто — сервер его не ведёт), renderer —
    источник, с которого приложение грузит рендерер (пусто — не сверяется)."""

    server: str
    question: str
    answer: str
    tool: str
    arguments: dict[str, Any]
    stats_url: str = ""
    renderer: str = ""

    def reply(self) -> Reply:
        call = ReplyCall(tool=self.tool, arguments=self.arguments)

        return Reply(
            question=self.question,
            turns=[ReplyTurn(calls=[call]), ReplyTurn(content=self.answer)],
        )


class AppUnderTest:
    """Приложение сервера стенда глазами теста: книга ответов по ходам
    cases, карточка хода в ленте, само приложение в странице-прокси
    песочницы, строки таблицы Prefab и счётчик вызовов сервера. Ошибки
    страницы чата копятся с открытия: их не должно быть."""

    CARD: ClassVar[str] = "[data-mcp-app]"
    WAIT_MS: ClassVar[float] = 60_000

    def __init__(
        self, chat: ChatPage, llm_port: int, cases: Sequence[AppCase], wait_ms: float
    ) -> None:
        self.chat = chat
        self.wait_ms = wait_ms
        self.errors: list[str] = []
        self.console: list[str] = []
        chat.page.on("pageerror", self._failed)
        chat.page.on("console", self._logged)
        chat.page.on("requestfailed", self._refused)
        replies: list[Reply] = []
        for case in cases:
            replies.append(case.reply())

        ReplyBookClient(StandUrl.of(llm_port)).register(replies)

    def _failed(self, error: Error) -> None:
        """Ошибка страницы чата со стеком: по минифицированному стеку видна
        цепочка функций. Playwright отдаёт ошибки всех кадров, а ошибка
        внутри окна приложения — дело сервера, не чата: по стеку без скрипта
        чата она идёт в консоль диагностики, не в ошибки страницы."""
        stack = str(error.stack or "")
        if stack and self.chat.base_url not in stack:
            self.console.append(f"app error: {error.message}\n{stack}")
            return

        self.errors.append(f"{error.message}\n{stack}")

    def _logged(self, message: ConsoleMessage) -> None:
        """Консоль кадров приложения — страницы-прокси и iframe рендерера;
        сообщения самой страницы чата не нужны."""
        where = str(message.location.get("url", ""))
        if where.startswith(self.chat.base_url):
            return

        self.console.append(f"{message.type} {where}: {message.text}")

    def _refused(self, request: Request) -> None:
        failure = request.failure
        self.console.append(f"request failed {request.url}: {failure}")

    @property
    def page(self) -> Page:
        return self.chat.page

    def drawn(self, app: FrameLocator, count: int) -> Locator:
        """Строки таблицы приложения, когда их стало count; иначе — ошибка с
        консолью всех кадров и упавшими запросами."""
        rows = self.rows(app)
        deadline = time.monotonic() + self.wait_ms / 1000
        while rows.count() != count:
            if time.monotonic() > deadline:
                shown = "\n".join(self.console[-60:])
                body = app.locator("body").inner_html()[:3000]
                tree = app.locator("body").aria_snapshot()[:1500]
                msg = (
                    f"the app draws {count} rows, got {rows.count()}; console and "
                    f"failed requests:\n{shown}\nbody of the app:\n{body}\n"
                    f"aria:\n{tree}\nresults the cards got:\n{self.results()}"
                )
                raise AssertionError(msg)

            self.page.wait_for_timeout(200)

        return rows

    def ask(self, case: AppCase) -> Locator:
        """Ход с вызовом инструмента case: карточка его приложения."""
        self.chat.ask(case.question)
        self.chat.await_idle(timeout_sec=120.0)
        self.page.get_by_text(case.answer).last.wait_for(timeout=self.wait_ms)

        return self.page.locator(self.CARD).last

    def app(self, card: Locator) -> FrameLocator:
        """Приложение в странице-прокси песочницы карточки."""
        app = card.frame_locator("iframe").first.frame_locator("iframe")
        app.locator("body").wait_for(timeout=self.wait_ms)

        return app

    def proxy(self, card: Locator) -> Locator:
        """iframe приложения внутри страницы-прокси."""
        return card.frame_locator("iframe").first.locator("iframe").first

    @staticmethod
    def rows(app: FrameLocator) -> Locator:
        """Строки таблицы Prefab в приложении."""
        return app.locator("table tbody tr")

    def results(self) -> list[str]:
        """Итоги вызовов (prop result) карточек ToolApp по журналу сокета:
        ключи и первые знаки structuredContent."""
        found: list[str] = []
        for frame in self.chat.log.of_event(ChatEvent.ELEMENT):
            if not isinstance(frame.payload, dict):
                continue

            if frame.payload.get("name") != "ToolApp":
                continue

            props = frame.payload.get("props")
            if not isinstance(props, dict) or "result" not in props:
                continue

            result = props["result"]
            structured = json.dumps(result.get("structuredContent"))[:300]
            found.append(f"{sorted(result)}: structuredContent={structured}")

        return found

    def stats(self, case: AppCase) -> dict[str, int]:
        """Вызовы инструментов сервера по его счётчику."""
        reply = self.page.request.get(case.stats_url)
        counts: dict[str, int] = {}
        for name, count in reply.json().items():
            counts[str(name)] = int(count)

        return counts

    def offered(self, llm_port: int) -> list[str]:
        """Инструменты, предложенные модели последним запросом."""
        reply = self.page.request.get(StandUrl.of(llm_port, "/requests"))
        requests = reply.json()["requests"]
        names: list[str] = []
        for tool in requests[-1].get("tools", []):
            names.append(str(tool["function"]["name"]))

        return names

    def check_clean(self) -> None:
        if self.errors:
            raise AssertionError(f"the chat page raised errors: {self.errors}")


@pytest.fixture(scope="module")
def fastmcp_stand(  # noqa: PLR0913 — стенд собирается из фикстур модуля
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
    apps_slot: AppsSlot,
    boba_mcp_stand: BobaMcpStand,
) -> Iterator[StandProcess]:
    """Стенд чата с серверами fastmcp[apps] контейнера boba-testapps —
    prefab, notes, genui — и с boba-mcp под именем boba; у каждого своя
    песочница на своём источнике. Виды отображения проверяются одинаково
    против чужого сервера и против своего."""
    servers: dict[str, dict[str, Any]] = {}
    for name, port in (
        ("prefab", AppServer.PREFAB),
        ("notes", AppServer.NOTES),
        ("genui", AppServer.GENUI),
    ):
        servers[name] = SandboxHost().drawn(apps_slot.host, port.value)

    servers["boba"] = {**boba_mcp_stand.server(), "apps": SandboxHost().apps()}
    config = StandConfig(
        workdir=stand_workdir / "chat-fastmcp",
        app=StandApp.AID,
        app_port=StandPorts().free(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-fastmcp",
        mcp_servers=servers,
    )
    process = StandProcess(config=config, log_path=stand_workdir / "fastmcp.log")
    try:
        process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
        yield process
    finally:
        process.stop()


@pytest.mark.xdist_group("fastmcp")
class TestFastmcpApps:
    """Виды интерфейсов fastmcp в harness без правок хоста: Prefab (таблица
    с поиском, рендерер статикой стенда без CDN), FastMCPApp (форма зовёт
    backend, список перерисован), генерация, разрешения и рамка ресурса,
    потолок размера ресурса. Те же проверки против boba-mcp: его стенд
    должен отвечать инструментами fake_sales (таблица продаж: 5 строк,
    Acme одна) и fake_notes (доска с формой и backend save) с объявлением
    вида."""

    BOBA_REASON: ClassVar[str] = (
        "boba-mcp declares no app: its stand has neither fake_sales with a Prefab "
        "table nor fake_notes with a backend, ToolCard carries no _meta.ui and "
        "the server serves no ui:// resources (plan items 6-10)"
    )
    SERVERS: ClassVar[list[Any]] = [
        pytest.param("prefab", id="fastmcp"),
        pytest.param(
            "boba",
            id="boba-mcp",
            marks=pytest.mark.xfail(strict=True, reason=BOBA_REASON),
        ),
    ]
    CHART: ClassVar[str] = (
        "from prefab_ui.components.charts import BarChart, ChartSeries\n"
        "from prefab_ui.app import PrefabApp\n"
        "with PrefabApp() as app:\n"
        "    BarChart(\n"
        "        data=[{'month': 'Jan', 'sales': 10}, {'month': 'Feb', 'sales': 25}],\n"
        "        series=[ChartSeries(data_key='sales')],\n"
        "    )\n"
    )

    MISSING_MS: ClassVar[float] = 15_000
    """Сколько ждать отрисовки, которой по xfail ещё нет: тот же ход у
    рабочего вида рисуется за 3–5 с, и заработавший вид успеет — strict
    покажет XPASS."""

    @pytest.fixture
    def wait_ms(self, request: pytest.FixtureRequest) -> float:
        """Ожидание отрисовки теста: xfail-случаю короткое, остальным полное."""
        if request.node.get_closest_marker("xfail") is None:
            return AppUnderTest.WAIT_MS

        return self.MISSING_MS

    @staticmethod
    def _table(server: str, apps_slot: AppsSlot) -> AppCase:
        tools = {"prefab": "mcp__prefab__sales", "boba": "mcp__boba__fake_sales"}
        stats = ""
        renderer = ""
        if server == "prefab":
            stats = f"http://{apps_slot.host}:{AppServer.PREFAB.value}/stats"
            renderer = apps_slot.renderer_url()

        return AppCase(
            server=server,
            question=f"Покажи продажи по клиентам ({server})",
            answer="Продажи — в таблице выше.",
            tool=tools[server],
            arguments={"region": "All"},
            stats_url=stats,
            renderer=renderer,
        )

    @staticmethod
    def _notes(server: str) -> AppCase:
        tools = {"prefab": "mcp__notes__notes_board", "boba": "mcp__boba__fake_notes"}

        return AppCase(
            server=server,
            question=f"Открой доску заметок ({server})",
            answer="Доска заметок — выше.",
            tool=tools[server],
            arguments={},
        )

    @staticmethod
    def _clipboard() -> AppCase:
        return AppCase(
            server="prefab",
            question="Покажи текст для копирования",
            answer="Текст — в карточке выше.",
            tool="mcp__prefab__clipboard",
            arguments={},
        )

    @pytest.mark.parametrize("server", SERVERS)
    def test_prefab_table_renders_and_filters_without_the_server(  # noqa: PLR0913 — ожидание отрисовки фикстурой по xfail
        self,
        fastmcp_stand: StandProcess,
        open_chat: OpenChat,
        llm_port: int,
        apps_slot: AppsSlot,
        server: str,
        wait_ms: float,
    ) -> None:
        """Карточка Prefab показывает строки таблицы; поиск фильтрует их в
        браузере — счётчик вызовов сервера не растёт. Чистота страницы чата
        при фильтрации — отдельный тест: там известная петля обновлений."""
        case = self._table(server, apps_slot)
        apps = AppUnderTest(open_chat(fastmcp_stand, ""), llm_port, [case], wait_ms)
        app = apps.app(apps.ask(case))
        rows = apps.drawn(app, 5)
        before: dict[str, int] = {}
        if case.stats_url:
            before = apps.stats(case)

        app.get_by_placeholder("Filter...").fill("Acme")

        expect(rows).to_have_count(1, timeout=apps.wait_ms)
        assert "Acme" in rows.first.inner_text()
        if case.stats_url:
            assert apps.stats(case) == before, "the search calls no tool"

    def test_prefab_filter_leaves_the_chat_page_without_errors(
        self,
        fastmcp_stand: StandProcess,
        open_chat: OpenChat,
        llm_port: int,
        apps_slot: AppsSlot,
        wait_ms: float,
    ) -> None:
        """Фильтрация таблицы приложения не роняет ошибок на страницу чата.
        Ошибка внутри кадра рендерера (prefab-ui 0.20.2 зацикливается на
        сбросе страницы таблицы при фильтре, React error #185 в renderer.js)
        — дело сервера: она идёт в диагностику, а не в ошибки страницы."""
        case = self._table("prefab", apps_slot)
        apps = AppUnderTest(open_chat(fastmcp_stand, ""), llm_port, [case], wait_ms)
        app = apps.app(apps.ask(case))
        rows = apps.drawn(app, 5)

        app.get_by_placeholder("Filter...").fill("Acme")
        expect(rows).to_have_count(1, timeout=apps.wait_ms)
        apps.page.wait_for_timeout(2_000)

        apps.check_clean()

    @pytest.mark.parametrize("server", SERVERS)
    def test_app_loads_with_cdn_blocked(  # noqa: PLR0913 — ожидание отрисовки фикстурой по xfail
        self,
        fastmcp_stand: StandProcess,
        open_chat: OpenChat,
        llm_port: int,
        apps_slot: AppsSlot,
        server: str,
        wait_ms: float,
    ) -> None:
        """Закрытый контур: без cdn.jsdelivr.net и fonts.googleapis.com
        карточка Prefab всё равно рисуется — рендерер грузится статикой с
        источника стенда."""
        case = self._table(server, apps_slot)
        chat = open_chat(fastmcp_stand, "")
        loaded: list[str] = []

        def abort(route: Route) -> None:
            route.abort()

        def seen(request: Request) -> None:
            loaded.append(request.url)

        chat.page.route("**/cdn.jsdelivr.net/**", abort)
        chat.page.route("**/fonts.googleapis.com/**", abort)
        chat.page.on("request", seen)
        apps = AppUnderTest(chat, llm_port, [case], wait_ms)
        app = apps.app(apps.ask(case))

        apps.drawn(app, 5)
        if case.renderer:
            from_stand = [url for url in loaded if url.startswith(case.renderer)]
            assert from_stand, loaded[-20:]

        apps.check_clean()

    @pytest.mark.parametrize("server", SERVERS)
    def test_fastmcpapp_form_saves_through_the_backend_and_rerenders(
        self,
        fastmcp_stand: StandProcess,
        open_chat: OpenChat,
        llm_port: int,
        server: str,
        wait_ms: float,
    ) -> None:
        """Форма доски зовёт backend save по имени из tools/list, список
        перерисован ответом backend'а; модели backend не предложен."""
        case = self._notes(server)
        apps = AppUnderTest(open_chat(fastmcp_stand, ""), llm_port, [case], wait_ms)
        app = apps.app(apps.ask(case))
        rows = apps.rows(app)
        rows.first.wait_for(timeout=apps.wait_ms)
        before = rows.count()
        note = f"note-{uuid4().hex[:6]}"

        app.get_by_placeholder("New note").fill(note)
        app.get_by_role("button", name="Save").click()

        expect(rows).to_have_count(before + 1, timeout=apps.wait_ms)
        expect(app.get_by_text(note)).to_be_visible(timeout=apps.wait_ms)
        offered = apps.offered(llm_port)
        assert case.tool in offered, offered
        backend = [
            name
            for name in offered
            if name.startswith(f"mcp__{server}__") and "save" in name
        ]
        assert not backend, offered
        apps.check_clean()

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "the stand has no Deno and no Pyodide offline: prefab_ui.sandbox cannot "
            "start, generate_prefab_ui answers with an error and the chart is never "
            "drawn (plan item 11)"
        ),
    )
    def test_generative_ui_result_is_drawn(
        self,
        fastmcp_stand: StandProcess,
        open_chat: OpenChat,
        llm_port: int,
        wait_ms: float,
    ) -> None:
        """Модель зовёт generate_prefab_ui с кодом графика: карточка рисует
        график тем же рендерером."""
        case = AppCase(
            server="genui",
            question="Нарисуй продажи по месяцам",
            answer="График — выше.",
            tool="mcp__genui__generate_prefab_ui",
            arguments={"code": self.CHART},
        )
        apps = AppUnderTest(open_chat(fastmcp_stand, ""), llm_port, [case], wait_ms)
        app = apps.app(apps.ask(case))

        chart = app.locator(".recharts-surface").first
        expect(chart).to_be_visible(timeout=apps.wait_ms)
        apps.check_clean()

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "the chat reads neither _meta.ui.permissions nor prefersBorder of the "
            "resource and [mcp.servers.<name>.apps] has no permissions allowlist, "
            "so the stand refuses the config, the inner iframe would get no allow "
            "attribute and the card would keep its border (plan item 2)"
        ),
    )
    def test_app_permissions_and_border_follow_the_resource(  # noqa: PLR0913 — свой стенд из фикстур модуля
        self,
        stand_workdir: Path,
        llm_port: int,
        fake_llm: None,
        stand_database: str,
        apps_slot: AppsSlot,
        open_chat: OpenChat,
        wait_ms: float,
    ) -> None:
        """Разрешение clipboardWrite из списка секции apps сервера попадает
        в allow внутреннего iframe; prefersBorder=false снимает рамку
        карточки."""
        server = SandboxHost().drawn(apps_slot.host, AppServer.PREFAB.value)
        server["apps"]["permissions"] = ["clipboardWrite"]
        config = StandConfig(
            workdir=stand_workdir / "chat-permitted",
            app=StandApp.AID,
            app_port=StandPorts().free(),
            llm_port=llm_port,
            db_name=stand_database,
            url_prefix="/boba-permitted",
            mcp_servers={"prefab": server},
        )
        process = StandProcess(config=config, log_path=stand_workdir / "permitted.log")
        try:
            process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
            case = self._clipboard()
            apps = AppUnderTest(open_chat(process, ""), llm_port, [case], wait_ms)
            card = apps.ask(case)
            apps.app(card)
            allow = apps.proxy(card).get_attribute("allow")
            border = card.evaluate("(el) => getComputedStyle(el).borderTopWidth")
        finally:
            process.stop()

        assert allow is not None, "the inner iframe carries allow"
        assert "clipboard-write" in allow, allow
        assert border == "0px", border

    def test_permissions_of_the_resource_stay_out_of_allow_without_the_config(
        self,
        fastmcp_stand: StandProcess,
        open_chat: OpenChat,
        llm_port: int,
        wait_ms: float,
    ) -> None:
        """Запрет по умолчанию: ресурс просит clipboardWrite, в конфиге
        сервера разрешений нет — внутренний iframe без allow."""
        case = self._clipboard()
        apps = AppUnderTest(open_chat(fastmcp_stand, ""), llm_port, [case], wait_ms)
        card = apps.ask(case)
        app = apps.app(card)

        expect(app.get_by_text("copy me")).to_be_visible(timeout=apps.wait_ms)
        assert apps.proxy(card).get_attribute("allow") is None
        apps.check_clean()

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "the chat has no ceiling on the size of a ui:// resource: the 12 MB page "
            "of tool huge is relayed to the browser and drawn instead of an error "
            "card (plan: размер и число ресурсов ui:// на карточку ограничены)"
        ),
    )
    def test_resource_over_the_ceiling_is_an_error_card_and_the_chat_stays_alive(
        self,
        fastmcp_stand: StandProcess,
        open_chat: OpenChat,
        llm_port: int,
        apps_slot: AppsSlot,
        wait_ms: float,
    ) -> None:
        huge = AppCase(
            server="prefab",
            question="Открой огромную страницу",
            answer="Страница — выше.",
            tool="mcp__prefab__huge",
            arguments={},
        )
        table = self._table("prefab", apps_slot)
        apps = AppUnderTest(
            open_chat(fastmcp_stand, ""), llm_port, [huge, table], wait_ms
        )

        card = apps.ask(huge)
        card.locator("[data-mcp-app-error]").wait_for(timeout=apps.wait_ms)

        app = apps.app(apps.ask(table))
        expect(apps.rows(app)).to_have_count(5, timeout=apps.wait_ms)
        apps.check_clean()
