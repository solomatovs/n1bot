"""Инструмент MCP-сервера в живой ленте чата.

Стенд поднимается с двумя MCP-серверами: обычным (официальный SDK, stdio),
который о boba не знает, и своим boba-mcp отдельным процессом по HTTP.
Фейковая модель зовёт их инструменты; проверяется то, что видит
пользователь: шаг инструмента в ленте, вложение у шага и сам файл по
ссылке вложения, итог своего сервера в его собственной модели результата.

Ошибки: своих не выпускает; расхождение — падение теста.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pytest
from chat_ui import BOOT_TIMEOUT_SEC, OpenChat

from boba.stand.ui.chat_page import ChatPage, StepKind
from boba.stand.ui.fake_llm import ScenarioName
from boba.stand.ui.socket_log import ChatEvent, StepField
from boba.stand.ui.stand import (
    REPO_ROOT,
    StandApp,
    StandConfig,
    StandProcess,
    free_port,
)

pytestmark = [pytest.mark.ui, pytest.mark.xdist_group("mcp-ui")]

SERVER = (
    REPO_ROOT / "packages" / "services" / "boba-mcp-client" / "tests"
) / "standard_server.py"

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
            "prefix": "boba_",
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
def mcp_stand(
    stand_workdir: Path,
    llm_port: int,
    fake_llm: None,
    stand_database: str,
    boba_mcp_stand: BobaMcpStand,
) -> Iterator[StandProcess]:
    """Стенд чата с двумя MCP-серверами: обычный по stdio (std_) и boba-mcp (boba_)."""
    server: dict[str, Any] = {
        "prefix": "std_",
        "connect_timeout_sec": 30.0,
        "call_timeout_sec": 60.0,
        "endpoint": {
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(SERVER), "stdio"],
        },
    }
    config = StandConfig(
        # не "mcp": это каталог сервиса boba-mcp общего стенда (StandConfig.service)
        workdir=stand_workdir / "chat-mcp",
        app=StandApp.CHAINLIT,
        app_port=free_port(),
        llm_port=llm_port,
        db_name=stand_database,
        url_prefix="/boba-mcp",
        mcp_servers={"standard": server, "boba": boba_mcp_stand.server()},
    )
    process = StandProcess(config=config, log_path=stand_workdir / "mcp-app.log")
    process.start(boot_timeout_sec=BOOT_TIMEOUT_SEC)
    try:
        yield process
    finally:
        process.stop()


def _ask(chat: ChatPage, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Ход с вызовом инструмента; отдаёт его шаг из журнала сокета."""
    request = json.dumps({"name": tool, "arguments": arguments})
    chat.ask(f"{ScenarioName.CALL.value} {request}")
    chat.await_idle(timeout_sec=120.0)

    step = chat.log.last_step(StepKind.TOOL.value)
    if step is None:
        raise AssertionError(
            f"no tool step of {tool} in the feed\n{chat.log.describe()}"
        )

    return step


class TestMcpToolInTheFeed:
    def test_text_tool_is_drawn_as_a_step(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(mcp_stand, "")

        step = _ask(chat, "std_add", {"a": 2, "b": 3})

        if "std_add" not in str(step.get(StepField.NAME.value)):
            raise AssertionError(f"the step is named after the mcp tool: {step}")
        if "5" not in str(step.get(StepField.OUTPUT.value)):
            raise AssertionError(f"the step shows the server's answer: {step}")

    def test_image_of_a_tool_is_attached_and_served(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(mcp_stand, "")

        step = _ask(chat, "std_picture", {})

        if "file saved to the workspace" not in str(step.get(StepField.OUTPUT.value)):
            raise AssertionError(f"the model is told where the file is: {step}")

        images: list[dict[str, Any]] = []
        for frame in chat.log.of_event(ChatEvent.ELEMENT):
            if not isinstance(frame.payload, dict):
                continue

            if frame.payload.get("mime") == "image/png":
                images.append(frame.payload)

        if len(images) != 1:
            raise AssertionError(
                f"the feed gets one image attachment: {images}\n{chat.log.describe()}"
            )

        url = str(images[0].get("url"))
        reply = chat.page.request.get(urljoin(chat.base_url, url))
        if reply.status != 200 or reply.body() != PNG:
            raise AssertionError(
                f"the attachment link serves the file of the workspace: "
                f"{reply.status} {url}"
            )

        name = str(images[0].get("name"))
        chat.page.get_by_text(name).first.wait_for(timeout=30_000)


class TestOwnServerInTheFeed:
    def test_tool_of_boba_mcp_is_drawn_with_its_result(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(mcp_stand, "")

        step = _ask(chat, "boba_fake_echo", {"text": "hi", "repeat": 2})

        if "boba_fake_echo" not in str(step.get(StepField.NAME.value)):
            raise AssertionError(f"the step is named after the tool: {step}")
        if "hi hi|t0ken" not in str(step.get(StepField.OUTPUT.value)):
            raise AssertionError(f"the step shows the result of the service: {step}")

    def test_body_of_boba_mcp_runs_as_the_chat_user(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        """Сервис узнаёт пользователя чата: логин и роли, с которыми тот
        вошёл в чат, приходят при входе proxy; тело исполняется от его имени."""
        chat = open_chat(mcp_stand, "")

        step = _ask(chat, "boba_fake_whoami", {})

        output = str(step.get(StepField.OUTPUT.value))
        if not output.startswith(f"{CHAT_LOGIN}|{CHAT_ROLE}|"):
            raise AssertionError(
                f"the body runs as the user of the chat with his roles: {step}"
            )

    def test_failed_body_of_boba_mcp_is_a_failed_step(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        chat = open_chat(mcp_stand, "")

        step = _ask(chat, "boba_fake_echo", {"text": "boom", "repeat": 1})

        if "fake backend is down" not in str(step.get(StepField.OUTPUT.value)):
            raise AssertionError(f"the step shows the cause of the failure: {step}")
        if not step.get(StepField.IS_ERROR.value):
            raise AssertionError(f"the step is marked as failed: {step}")

    def test_arguments_look_the_same_after_the_page_is_reopened(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        """Аргументы вызова после повторного захода на страницу выглядят как в
        живой ленте: запрос блоком кода sql, а не сырым json. Шаги треда
        страница запрашивает отдельно от сессии — так же, как здесь."""
        chat = open_chat(mcp_stand, "")

        live = _ask(chat, "boba_fake_query", {"sql": "select 42", "limit": 3})

        shown = str(live.get(StepField.INPUT.value))
        if not shown.startswith("```sql\nselect 42\n```"):
            raise AssertionError(f"the live step shows the query as sql: {shown!r}")

        thread_id = str(live.get(StepField.THREAD_ID.value))
        chat.page.reload()
        steps = chat.page.evaluate(
            """async (url) => {
                const response = await fetch(url, {credentials: "include"});
                const thread = await response.json();
                return thread.steps;
            }""",
            f"{mcp_stand.config.url_prefix}/project/thread/{thread_id}",
        )

        replayed: list[str] = []
        for step in steps:
            if step.get(StepField.TYPE.value) != StepKind.TOOL.value:
                continue

            replayed.append(str(step.get(StepField.INPUT.value)))

        if replayed != [shown]:
            raise AssertionError(
                f"the reopened page shows the arguments as the live feed: "
                f"{replayed!r} != {shown!r}"
            )

    def test_journal_of_a_boba_mcp_call_opens_in_the_panel(
        self, mcp_stand: StandProcess, open_chat: OpenChat
    ) -> None:
        """Журнал лежит у сервиса: кнопка шага открывает его в панели, текст
        панель читает окном через клиента MCP."""
        chat = open_chat(mcp_stand, "")

        _ask(chat, "boba_fake_echo", {"text": "journal", "repeat": 1})

        tool = chat.expand_last_tool()
        tool.locator('[aria-label="Show tool output"]').click()

        side = chat.page.locator("#side-view-content")
        side.get_by_text("echo progress: journal").first.wait_for(timeout=30_000)
