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
приложение MCP Apps у шага вызова и его изоляция от страницы чата.

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
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from chat_ui import BOOT_TIMEOUT_SEC, OpenChat
from playwright.sync_api import Browser, FrameLocator, Locator, Page

from boba.mcp_client.client import McpServerConfig
from boba.stand.ui.chat_page import ChatPage, Selector, StepKind
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.socket_log import ChatEvent, StepField
from boba.stand.ui.stand import (
    REPO_ROOT,
    StandApp,
    StandConfig,
    StandProcess,
    StandUrl,
    free_port,
)

pytestmark = [pytest.mark.ui, pytest.mark.xdist_group("mcp-ui")]

CHAT_LOGIN = "admin"
CHAT_ROLE = "ADM"
"""Логин пользователя стенда чата: под ним открыт чат в браузере."""

RUNNING = "○"
"""Кружок идущего вызова в названии шага."""

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
        self.port = free_port()
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
def mcp_stand(  # noqa: PLR0913 — стенд собирается из фикстур модуля
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
    boba_mcp_stand: BobaMcpStand,
    standard: McpServerConfig,
) -> Iterator[StandProcess]:
    """Стенд чата с MCP-серверами: обычный по stdio, boba-mcp и обычный по
    HTTP — записью apps с секцией apps (страница-прокси песочницы на своём
    порту) и записью plain без неё."""
    server = standard.model_dump(mode="json", exclude_none=True)
    apps_port = free_port()
    sandbox_port = free_port()
    script = REPO_ROOT / "packages/services/boba-mcp-client/tests/standard_server.py"
    pages = REPO_ROOT / "packages/apps/boba-aid/assets/mcp-apps"
    apps_server = subprocess.Popen(
        [sys.executable, str(script), "streamable-http", str(apps_port)]
    )
    # страница-прокси — на своём источнике, как её отдаёт nginx хоста сервера
    serve = ["-m", "http.server", str(sandbox_port), "--bind", "127.0.0.1"]
    sandbox_server = subprocess.Popen(
        [sys.executable, *serve, "--directory", str(pages)]
    )
    plain = {
        "connect_timeout_sec": 30.0,
        "call_timeout_sec": 60.0,
        "endpoint": {
            "transport": "streamable-http",
            "scheme": "http",
            "host": "127.0.0.1",
            "port": apps_port,
            "path": "/mcp",
        },
    }
    drawn = {
        **plain,
        "apps": {"sandbox_url": f"http://localhost:{sandbox_port}/sandbox.html"},
    }
    config = StandConfig(
        # не "mcp": это каталог сервиса boba-mcp общего стенда (StandConfig.service)
        workdir=stand_workdir / "chat-mcp",
        app=StandApp.AID,
        app_port=free_port(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-mcp",
        mcp_servers={
            "standard": server,
            "boba": boba_mcp_stand.server(),
            "apps": drawn,
            "plain": plain,
        },
    )
    process = StandProcess(config=config, log_path=stand_workdir / "mcp-app.log")
    try:
        for port in (apps_port, sandbox_port):
            deadline = time.monotonic() + BobaMcpStand.BOOT_SEC
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                        break
                except OSError as exc:
                    if time.monotonic() > deadline:
                        msg = (
                            f"the apps server ({apps_server.args}) or the sandbox page "
                            f"({sandbox_server.args}) is not listening on port {port} "
                            f"within {BobaMcpStand.BOOT_SEC}s: {exc}"
                        )
                        raise AssertionError(msg) from exc

                    time.sleep(0.2)

        process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
        yield process
    finally:
        process.stop()
        apps_server.terminate()
        apps_server.wait(timeout=10)
        sandbox_server.terminate()
        sandbox_server.wait(timeout=10)


def _ask(chat: ChatPage, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Ход с вызовом инструмента; отдаёт его законченный шаг из журнала
    сокета. Конец хода ждётся по самому шагу: пара task_start/task_end от
    старта чата может прийти уже после вопроса."""
    request = json.dumps({"name": tool, "arguments": arguments})
    chat.ask(f"{ScenarioName.CALL.value} {request}")
    chat.await_idle(timeout_sec=120.0)

    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        chat.page.wait_for_timeout(100)
        step = chat.log.last_step(StepKind.TOOL.value)
        if step is None:
            continue

        if str(step.get(StepField.NAME.value)).startswith(RUNNING):
            continue

        return step

    raise AssertionError(f"no finished tool step of {tool}\n{chat.log.describe()}")


def _app(chat: ChatPage) -> tuple[Locator, FrameLocator]:
    """Ход с вызовом board сервера apps: шаг вызова и внутренний iframe его
    приложения, нарисовавшего разметку модели."""
    _ask(chat, "mcp__apps__board", {"markup": '<b id="cells">three cells</b>'})
    tool = chat.expand_last_tool()
    tool.locator("iframe").first.wait_for(timeout=30_000)
    inner = tool.frame_locator("iframe").first.frame_locator("iframe")
    inner.locator("#cells").wait_for(timeout=30_000)

    return tool, inner


class TestMcpToolInTheFeed:
    def test_text_tool_is_drawn_as_a_step(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(mcp_stand, "")

        step = _ask(chat, "mcp__standard__add", {"a": 2, "b": 3})

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

        step = _ask(chat, "mcp__standard__picture", {})

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
        url = f"{chat.base_url}/canvas/{thread_id}/upload/{name}"
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

        step = _ask(chat, "mcp__standard__blocks", {"empty": False})

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
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        """Look-тест дерева JSON: корень раскрыт, вложенное свёрнуто и
        раскрывается кликом; строка с разметкой видна текстом и ничего не
        исполняет."""
        chat = open_chat(mcp_stand, "")
        page = chat.page

        _ask(chat, "mcp__standard__blocks", {"empty": False})
        step = chat.expand_last_tool()

        tree = step.locator("[data-json-tree]").last
        tree.wait_for(state="visible", timeout=30_000)
        rows = tree.locator("[data-json-node] [data-json-node]").first

        folded = tree.inner_text()
        if '"total": 1' not in folded:
            raise AssertionError(f"the root is expanded: {folded!r}")
        if "1 item ]" not in folded:
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
        folded_back = tree.get_by_text(re.compile(r"\b1 item\b"))
        folded_back.wait_for(state="visible", timeout=10_000)
        if "1 item ]" not in tree.inner_text():
            raise AssertionError(f"the array folds back: {tree.inner_text()!r}")


class TestOwnServerInTheFeed:
    def test_tool_of_boba_mcp_is_drawn_with_its_result(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(mcp_stand, "")

        step = _ask(chat, "mcp__boba__fake_echo", {"text": "hi", "repeat": 2})

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

        step = _ask(chat, "mcp__boba__fake_whoami", {})

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

        step = _ask(chat, "mcp__boba__fake_scope", {})

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

        step = _ask(chat, "mcp__boba__fake_echo", {"text": "boom", "repeat": 1})

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

        live = _ask(chat, "mcp__boba__fake_query", {"sql": "select 42", "limit": 3})

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

        _ask(chat, "mcp__boba__fake_echo", {"text": "journal", "repeat": 1})

        tool = chat.expand_last_tool()
        tool.locator('[aria-label="Show tool output"]').click()

        side = chat.page.locator("#side-view-content")
        side.get_by_text("echo progress: journal").first.wait_for(timeout=30_000)


class TestToolApps:
    """Приложение MCP Apps у шага вызова: рисуется в песочнице на хосте
    сервера, его кнопки зовут инструменты своего сервера только с "app" в
    visibility, ui/message доходит до агента, контекст приложения уходит
    модели со следующим вопросом; страница и куки чата скрипту приложения
    недоступны. Сервер без секции apps показывает итог блоками."""

    @staticmethod
    def _llm_requests(chat: ChatPage, llm_port: int, marker: str) -> list[str]:
        """Сообщения запросов фейковой модели с marker, JSON-текстом."""
        reply = chat.page.request.get(StandUrl.of(llm_port, "/requests"))
        found: list[str] = []
        for payload in reply.json()["requests"]:
            messages = json.dumps(payload.get("messages"))
            if marker in messages:
                found.append(messages)

        return found

    def test_app_draws_the_call_and_calls_its_server(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(mcp_stand, "")
        _tool, inner = _app(chat)
        answer = inner.locator("#answer")

        inner.locator("#result").get_by_text("the board is drawn by its app").wait_for()
        inner.locator("#call-app").click()
        answer.get_by_text("answer: app_only answered").wait_for(timeout=30_000)
        inner.locator("#call-model").click()
        answer.get_by_text("error:").wait_for(timeout=30_000)

        refused = answer.inner_text()
        if "model_only" not in refused:
            raise AssertionError(f"the app is refused the tool of the model: {refused}")

    def test_app_sees_neither_the_cookies_nor_the_page_of_the_chat(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(mcp_stand, "")
        _tool, inner = _app(chat)

        probe = inner.locator("#probe").inner_text()

        if probe != "cookies: none; chat page: hidden":
            raise AssertionError(f"the app script is cut off from the chat: {probe}")

    def test_message_and_context_of_the_app_reach_the_agent(
        self, mcp_stand: StandProcess, open_chat: OpenChat, llm_port: int
    ) -> None:
        """ui/message — вопрос агенту от пользователя; ui/update-model-context
        модель получает со следующим вопросом, а лента вопроса его не
        показывает."""
        chat = open_chat(mcp_stand, "")
        _tool, inner = _app(chat)

        inner.locator("#send-context").click()
        inner.locator("#answer").get_by_text("answer:").wait_for(timeout=30_000)
        inner.locator("#send-message").click()
        chat.page.get_by_text("the board asks to go on").first.wait_for(timeout=60_000)
        chat.await_idle(timeout_sec=120.0)

        asked = self._llm_requests(chat, llm_port, "the board asks to go on")
        if not asked:
            raise AssertionError("ui/message of the app reaches the model")

        if "the board shows three cells" not in asked[-1]:
            raise AssertionError(f"the context goes with the question: {asked[-1]}")

        if '\\"cells\\": 3' not in asked[-1]:
            raise AssertionError(f"the structured context goes as json: {asked[-1]}")

        questions = [
            str(step.get(StepField.OUTPUT.value))
            for step in chat.log.steps_of_type(StepKind.USER.value)
        ]
        if any("the board shows" in question for question in questions):
            raise AssertionError(
                f"the context is not a part of the question: {questions}"
            )

    def test_server_without_apps_shows_the_blocks(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(mcp_stand, "")

        step = _ask(chat, "mcp__plain__board", {"markup": "plain"})
        tool = chat.expand_last_tool()

        output = str(step.get(StepField.OUTPUT.value))
        if "board markup: plain" not in output:
            raise AssertionError(f"the server answers with blocks: {step}")

        if tool.locator("iframe").count():
            raise AssertionError("a server without apps draws no app")


class TestToolAppLook:
    """Вид приложения у шага: внешний iframe на источнике песочницы с
    разрешениями стандарта, внутренний — без allow-same-origin и с CSP;
    высота по size-changed приложения, узкое окно без горизонтальной
    прокрутки, тема чата в контексте хоста."""

    def test_app_frames_follow_the_standard(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(mcp_stand, "")
        tool, inner = _app(chat)
        outer = tool.locator("iframe").first
        proxy = tool.frame_locator("iframe").first.locator("iframe").first

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

        dark = chat.page.evaluate("document.documentElement.classList.contains('dark')")
        theme = "light"
        if dark:
            theme = "dark"

        inner.locator("#theme").get_by_text(f"theme: {theme}").wait_for()
        box = outer.bounding_box()
        if box is None:
            raise AssertionError("the frame of the app is laid out on the page")

        if not 0 < box["height"] < 600:
            raise AssertionError(f"the frame takes the height of the app: {box}")

    def test_app_follows_the_theme_and_the_narrow_window(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(mcp_stand, "")
        chat.page.set_viewport_size({"width": 420, "height": 800})
        _tool, inner = _app(chat)

        dark = chat.page.evaluate("document.documentElement.classList.toggle('dark')")
        theme = "light"
        if dark:
            theme = "dark"

        inner.locator("#theme").get_by_text(f"theme: {theme}").wait_for(timeout=10_000)

        width = "document.documentElement.scrollWidth"
        shown = "document.documentElement.clientWidth"
        scroll = chat.page.evaluate(f"[{width}, {shown}]")
        if scroll[0] > scroll[1]:
            raise AssertionError(f"no horizontal scroll in a narrow window: {scroll}")

        # текст приложения берёт тему хоста: светлый на тёмном и наоборот
        light = inner.locator("#board").evaluate(
            "el => { const [r, g, b] = getComputedStyle(el).color"
            ".match(/\\d+/g).map(Number); return (r + g + b) / 3 > 128; }"
        )
        if light is not dark:
            raise AssertionError(
                f"the text of the app follows the {theme} theme of the host"
            )

    def test_frame_grows_with_the_app(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        """Приложение дописало строку ответа — рамка выросла по его
        size-changed, нижняя строка не обрезана."""
        chat = open_chat(mcp_stand, "")
        tool, inner = _app(chat)
        outer = tool.locator("iframe").first
        before = outer.bounding_box()

        inner.locator("#call-app").click()
        answer = inner.locator("#answer")
        answer.get_by_text("answer: app_only answered").wait_for(timeout=30_000)

        clipped = "b => b.scrollHeight - window.innerHeight"
        rest = inner.locator("body").evaluate(clipped)
        for _ in range(20):
            if rest <= 1:
                break
            chat.page.wait_for_timeout(250)
            rest = inner.locator("body").evaluate(clipped)

        after = outer.bounding_box()
        if rest > 1:
            raise AssertionError(f"the app is clipped by {rest}px: {before} -> {after}")
        if before is None or after is None:
            raise AssertionError("the frame of the app is laid out on the page")
        if after["height"] <= before["height"]:
            raise AssertionError(f"the frame grows with the app: {before} -> {after}")


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
