"""Процесс `boba acp` на стенде клиентом SDK agent-client-protocol: модель —
FakeLlmApp, инструменты — standard_server.py по streamable HTTP. Сценарии:
рукопожатие и возможности; ход с инструментом и порядок «все уведомления
до ответа»; разрешение с запоминанием правила и отказ; отмена хода
`session/cancel` и `$/cancel_request`, в том числе с висящим вопросом;
второй `prompt` в очереди; отказы на чужие блоки, неизвестную сессию,
относительный `cwd` и сервер sse; отчёт о ходе вызова в `_meta`; секреты
только в файле 0600 — не в argv, окружении, журнале и кадрах; stdout
содержит только кадры JSON-RPC, логи — в stderr; SIGTERM и конец ввода
завершают процесс с кодом 0 и сброшенным журналом. История: `session/load`
второго процесса воспроизводит беседу теми же уведомлениями, что живой ход
(один рендерер FeedView на оба), и всё до ответа; `session/resume` без
воспроизведения; `session/close`; `session/list` страницами с курсором и
фильтром `cwd`; `session/delete` повторно — успех; неизвестная сессия —
-32002. Настройки сессии: `modes` и `configOptions` в ответах
`session/new|load|resume`, `set_mode`/`set_config_option` с уведомлениями,
смена профиля модели между ходами и отказ при идущем ходе. Служебные
уведомления: `usage_update` после хода и после сжатия, `session_info_update`
с заголовком после первого хода и при `load`. Ветвление: `session/fork` с
`boba/upToUuid` даёт сессию с беседой до этого вопроса, без ключа — копию
всей сессии, срез на первом вопросе — пустую сессию; исходный журнал не
меняется; отказы на неизвестную сессию, неизвестный uuid среза и негодную
`_meta`; ответ `fork` несёт `modes` и `configOptions`."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import stat
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar, TypeVar

import pytest
import uvicorn
from acp import Client, connect_to_agent, resource_link_block, text_block
from acp.client.router import build_client_router
from acp.connection import Connection, StreamDirection
from acp.connection import StreamEvent as FrameEvent
from acp.exceptions import RequestError
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    AllowedOutcome,
    ClientCapabilities,
    ConfigOptionUpdate,
    CreateElicitationResponse,
    CreateTerminalResponse,
    CurrentModeUpdate,
    DeniedOutcome,
    ElicitationMode,
    EnvVariable,
    HttpHeader,
    HttpMcpServer,
    ImageContentBlock,
    Implementation,
    KillTerminalResponse,
    PermissionOption,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    RequestPermissionResponse,
    SessionConfigOptionSelect,
    SessionConfigSelectGroup,
    SessionInfoUpdate,
    SseMcpServer,
    TerminalOutputResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
    UserMessageChunk,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)
from acp.schema import ContentChunk as AcpContentChunk

from boba.cli.acp import (
    ConfigOptionId,
    MetaKey,
    PermissionChoice,
    StopReasonName,
    ToolKindName,
)
from boba.stand.ui.fake_llm import FailureName, FakeLlmApp, ScenarioName
from boba.stand.ui.stand import free_port

pytestmark = pytest.mark.anyio

MCP_SERVER = (
    Path(__file__).resolve().parents[3]
    / "services"
    / "boba-mcp-client"
    / "tests"
    / "standard_server.py"
)
READ_TIMEOUT_SEC = 30.0
API_KEY = "sk-acp-test-secret-key"
OWNER_ONLY = 0o600
INVALID_PARAMS = -32602
RESOURCE_NOT_FOUND = -32002
INTERNAL_ERROR = -32603
METHOD_NOT_FOUND = -32601
Document = dict[str, Any]
U = TypeVar("U")


def call(*specs: dict[str, object]) -> str:
    if len(specs) == 1:
        return f"{ScenarioName.CALL.value} {json.dumps(specs[0])}"

    return f"{ScenarioName.CALL.value} {json.dumps(list(specs))}"


class FakeModel:
    """FakeLlmApp под uvicorn в цикле теста; `estimate_usage` — учёт токенов
    по знакам, как у настоящего провайдера, нужен сценариям сжатия;
    `unmarked` — сценарий для вопроса без маркера (чужие клиенты и TCK шлют
    обычный текст)."""

    def __init__(
        self, estimate_usage: bool = False, unmarked: ScenarioName | None = None
    ) -> None:
        self.app = FakeLlmApp(
            token_delay_sec=0.0, estimate_usage=estimate_usage, unmarked=unmarked
        )
        self.port = free_port()
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app.asgi(),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                timeout_graceful_shutdown=1,
            )
        )
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.02)

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await self._task

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


class McpStand:
    """standard_server.py по streamable HTTP в субпроцессе."""

    PATH: ClassVar[str] = "/mcp"

    def __init__(self) -> None:
        self.port = free_port()
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(MCP_SERVER),
            "streamable-http",
            str(self.port),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        for _ in range(200):
            if self._accepts():
                return

            await asyncio.sleep(0.05)

        msg = f"the MCP stand did not open port {self.port}"
        raise AssertionError(msg)

    def _accepts(self) -> bool:
        with socket.socket() as sock:
            sock.settimeout(0.1)
            try:
                sock.connect(("127.0.0.1", self.port))
            except OSError:
                return False

            return True

    async def stop(self) -> None:
        if self._process is None or self._process.returncode is not None:
            return

        self._process.terminate()
        await asyncio.wait_for(self._process.wait(), READ_TIMEOUT_SEC)

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.PATH}"

    def server(self, name: str = "stand") -> HttpMcpServer:
        return HttpMcpServer(
            type="http",
            name=name,
            url=self.url(),
            headers=[HttpHeader(name="Authorization", value="Bearer stand-token")],
        )


class FeaturelessClient(Client):
    """Реализация Client SDK без файловой системы, терминала и форм: эти
    запросы клиент не обслуживает и отвечает «метод не найден». Базовый
    класс клиентов стенда."""

    async def write_text_file(
        self, session_id: str, path: str, content: str, **kwargs: Any
    ) -> WriteTextFileResponse | None:
        raise RequestError.method_not_found("fs/write_text_file")

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        raise RequestError.method_not_found("fs/read_text_file")

    async def create_terminal(  # noqa: PLR0913 — сигнатура протокола SDK
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[EnvVariable] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> TerminalOutputResponse:
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> ReleaseTerminalResponse | None:
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> WaitForTerminalExitResponse:
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> KillTerminalResponse | None:
        raise RequestError.method_not_found("terminal/kill")

    async def create_elicitation(
        self, message: str, mode: ElicitationMode, **kwargs: Any
    ) -> CreateElicitationResponse:
        raise RequestError.method_not_found("elicitation/create")

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        return

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(f"_{method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return

    def on_connect(self, conn: Any) -> None:
        return


class RecordingClient(FeaturelessClient):
    """Копит уведомления, отвечает на запросы разрешения выбранным
    вариантом либо отменой; `hold` задерживает ответ."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []
        self.permissions: list[ToolCallUpdate] = []
        self.options: list[Sequence[PermissionOption]] = []
        self.choice: PermissionChoice | None = PermissionChoice.ALLOW_ONCE
        self.hold: asyncio.Event | None = None
        self._changed = asyncio.Event()

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append((session_id, update))
        self._changed.set()

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        self.permissions.append(tool_call)
        self.options.append(options)
        self._changed.set()
        if self.hold is not None:
            await self.hold.wait()

        if self.choice is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=self.choice.value)
        )

    async def next_permission(self, seen: int) -> ToolCallUpdate:
        while len(self.permissions) <= seen:
            self._changed.clear()
            await asyncio.wait_for(self._changed.wait(), READ_TIMEOUT_SEC)

        return self.permissions[seen]

    def starts(self) -> list[ToolCallStart]:
        found: list[ToolCallStart] = []
        for _, update in self.updates:
            if isinstance(update, ToolCallStart):
                found.append(update)

        return found

    def progress_of(self, tool_call_id: str) -> list[ToolCallProgress]:
        found: list[ToolCallProgress] = []
        for _, update in self.updates:
            if not isinstance(update, ToolCallProgress):
                continue

            if update.tool_call_id == tool_call_id:
                found.append(update)

        return found

    def chunks(self) -> list[AcpContentChunk]:
        found: list[AcpContentChunk] = []
        for _, update in self.updates:
            if isinstance(update, AcpContentChunk):
                found.append(update)

        return found

    def updates_of(self, session_id: str) -> list[Any]:
        found: list[Any] = []
        for seen, update in self.updates:
            if seen == session_id:
                found.append(update)

        return found

    def of(self, kind: type[U]) -> list[U]:
        found: list[U] = []
        for _, update in self.updates:
            if isinstance(update, kind):
                found.append(update)

        return found


class FeedView:
    """Лента по уведомлениям сессии — один рендерер для живого хода и для
    воспроизведения: куски одного сообщения склеиваются, у вызова остаются
    заголовок, вид, аргументы, конечный статус, текст и `rawOutput`."""

    def render(self, updates: Sequence[Any]) -> list[tuple[Any, ...]]:
        items: list[tuple[Any, ...]] = []
        calls: dict[str, dict[str, Any]] = {}
        for update in updates:
            if isinstance(update, UserMessageChunk):
                items.append(("user", update.message_id, self._text_of(update)))
                continue

            if isinstance(update, AgentThoughtChunk):
                self._glue(items, "thought", update)
                continue

            if isinstance(update, AgentMessageChunk):
                self._glue(items, "agent", update)
                continue

            if isinstance(update, ToolCallStart):
                state = {
                    "id": update.tool_call_id,
                    "title": update.title,
                    "kind": update.kind,
                    "raw_input": update.raw_input,
                    "status": update.status,
                    "text": None,
                    "raw_output": None,
                }
                calls[update.tool_call_id] = state
                items.append(("tool", state))
                continue

            if isinstance(update, ToolCallProgress):
                state = calls[update.tool_call_id]
                state["status"] = update.status
                if update.content:
                    state["text"] = result_text(update)

                if update.raw_output is not None:
                    state["raw_output"] = update.raw_output

        return [self._frozen(item) for item in items]

    def without_user(
        self, rendered: Sequence[tuple[Any, ...]]
    ) -> list[tuple[Any, ...]]:
        return [item for item in rendered if item[0] != "user"]

    def user_texts(self, rendered: Sequence[tuple[Any, ...]]) -> list[str]:
        return [item[2] for item in rendered if item[0] == "user"]

    @staticmethod
    def _text_of(update: AcpContentChunk) -> str:
        block = update.content
        assert isinstance(block, TextContentBlock)

        return block.text

    def _glue(
        self, items: list[tuple[Any, ...]], kind: str, update: AcpContentChunk
    ) -> None:
        text = self._text_of(update)
        if items and items[-1][0] == kind and items[-1][1] == update.message_id:
            items[-1] = (kind, update.message_id, items[-1][2] + text)
            return

        items.append((kind, update.message_id, text))

    @staticmethod
    def _frozen(item: tuple[Any, ...]) -> tuple[Any, ...]:
        if item[0] != "tool":
            return item

        state = item[1]

        return (
            "tool",
            state["id"],
            state["title"],
            state["kind"],
            json.dumps(state["raw_input"], sort_keys=True),
            state["status"],
            state["text"],
            json.dumps(state["raw_output"], sort_keys=True),
        )


def result_text(update: ToolCallProgress) -> str:
    """Текст первого блока содержимого обновления вызова."""
    assert update.content is not None
    content = update.content[0]
    assert content.type == "content"
    block = content.content
    assert isinstance(block, TextContentBlock)

    return block.text


class Frames:
    """Наблюдатель кадров соединения: порядок входящих и исходящих."""

    def __init__(self) -> None:
        self.events: list[FrameEvent] = []

    def __call__(self, event: FrameEvent) -> None:
        self.events.append(event)

    def request_id(self, method: str, index: int = 0) -> int:
        found: list[int] = []
        for event in self.events:
            if event.direction is not StreamDirection.OUTGOING:
                continue

            message = event.message
            if message.get("method") == method and "id" in message:
                found.append(int(message["id"]))

        return found[index]

    def position_of_response(self, request_id: int) -> int:
        for position, event in enumerate(self.events):
            if event.direction is not StreamDirection.INCOMING:
                continue

            message = event.message
            if "method" in message:
                continue

            if message.get("id") == request_id:
                return position

        msg = f"no response to request {request_id} seen"
        raise AssertionError(msg)

    def incoming_notifications(self, before: int) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for event in self.events[:before]:
            if event.direction is not StreamDirection.INCOMING:
                continue

            if "method" in event.message and "id" not in event.message:
                found.append(event.message)

        return found

    def incoming_after(self, position: int) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for event in self.events[position + 1 :]:
            if event.direction is StreamDirection.INCOMING:
                found.append(event.message)

        return found


class AcpProcess:
    """Запущенный `boba acp`: соединение клиента SDK поверх его stdio, сырые
    строки stdout и stderr в списках. Команда по умолчанию — модуль из venv;
    бинарник передаётся явно."""

    MODULE: ClassVar[str] = "boba.cli.app"
    LINE_LIMIT: ClassVar[int] = 16 * 1024 * 1024

    def __init__(
        self,
        root: Path,
        model: FakeModel,
        command: Sequence[str] | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if command is None:
            command = (sys.executable, "-m", self.MODULE)

        if environment is None:
            environment = {}

        self._command = list(command)
        self._root = root
        self._model = model
        self._environment = dict(environment)
        self._process: asyncio.subprocess.Process | None = None
        self.raw_stdout: list[str] = []
        self.stderr: list[str] = []
        self.frames = Frames()
        self._tasks: list[asyncio.Task[None]] = []
        self._reader = asyncio.StreamReader(limit=self.LINE_LIMIT)

    @property
    def process(self) -> asyncio.subprocess.Process:
        assert self._process is not None
        return self._process

    def settings_path(self) -> Path:
        return self._root / "settings.json"

    def journal(self, session_id: str) -> list[Document]:
        path = self._root / "history" / f"{session_id}.jsonl"
        found: list[Document] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            found.append(json.loads(raw))

        return found

    def settings_document(
        self, mode: str = "default", allow_bypass: bool = False
    ) -> Document:
        return {
            "model": "fake",
            "models": {"fake": self.profile_document("fake-model")},
            "history": {"kind": "jsonl", "root": str(self._root / "history")},
            "permissions": {"defaultMode": mode, "allowBypass": allow_bypass},
        }

    def profile_document(
        self,
        model: str,
        sampling: Mapping[str, Any] | None = None,
        context_window: int = 32000,
        max_output_tokens: int = 4096,
        chars_per_token: int = 3,
    ) -> Document:
        """Профиль `models.<имя>` над фейковой моделью."""
        if sampling is None:
            sampling = {}

        return {
            "provider": {
                "kind": "openai",
                "baseUrl": self._model.base_url(),
                "apiKey": API_KEY,
            },
            "model": model,
            "sampling": dict(sampling),
            "contextWindow": context_window,
            "maxOutputTokens": max_output_tokens,
            "charsPerToken": chars_per_token,
        }

    def write_settings(self, document: Document) -> Path:
        path = self.settings_path()
        path.write_text(json.dumps(document), encoding="utf-8")
        path.chmod(OWNER_ONLY)

        return path

    async def spawn(self, *extra: str) -> None:
        """Запускает процесс по уже записанному файлу настроек."""
        environment = {
            "HOME": str(self._root / "home"),
            "PATH": os.environ["PATH"],
            **self._environment,
        }
        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            "acp",
            "--settings",
            str(self.settings_path()),
            "--setting-sources",
            "user",
            *extra,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self.LINE_LIMIT,
            env=environment,
            cwd=str(self._root),
        )
        self._tasks.append(asyncio.create_task(self._tee_stdout()))
        self._tasks.append(asyncio.create_task(self._drain_stderr()))

    async def start(
        self, client: Client, document: Document | None = None, *extra: str
    ) -> Any:
        """Файл настроек, процесс и соединение клиента SDK."""
        if document is None:
            document = self.settings_document()

        self.write_settings(document)
        await self.spawn(*extra)

        return self.connect(client)

    def connect(self, client: Client) -> Any:
        stdin = self.process.stdin
        assert stdin is not None

        return connect_to_agent(
            client,
            stdin,
            self._reader,
            use_unstable_protocol=True,
            observers=[self.frames],
        )

    def raw_connection(self, client: Client) -> Connection:
        """Соединение SDK без обёртки: кадры собираются вручную."""
        stdin = self.process.stdin
        assert stdin is not None

        return Connection(
            build_client_router(client, use_unstable_protocol=True),
            stdin,
            self._reader,
            observers=[self.frames],
        )

    async def close_stdin(self) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        stdin.close()
        await stdin.wait_closed()

    async def wait(self) -> int:
        code = await asyncio.wait_for(self.process.wait(), READ_TIMEOUT_SEC)
        for task in self._tasks:
            await task

        return code

    async def kill(self) -> None:
        if self._process is None or self._process.returncode is not None:
            return

        self._process.kill()
        await asyncio.wait_for(self._process.wait(), READ_TIMEOUT_SEC)

    def assert_stdout_is_frames_only(self) -> None:
        assert self.raw_stdout, self.stderr[-10:]
        for line in self.raw_stdout:
            frame = json.loads(line)
            assert frame["jsonrpc"] == "2.0", line

    async def _tee_stdout(self) -> None:
        stdout = self.process.stdout
        assert stdout is not None
        while True:
            raw = await stdout.readline()
            if not raw:
                self._reader.feed_eof()
                return

            self.raw_stdout.append(raw.decode("utf-8").rstrip("\n"))
            self._reader.feed_data(raw)

    async def _drain_stderr(self) -> None:
        stderr = self.process.stderr
        assert stderr is not None
        while True:
            raw = await stderr.readline()
            if not raw:
                return

            self.stderr.append(raw.decode("utf-8", errors="replace").rstrip("\n"))


@pytest.fixture
async def boba(model: FakeModel, tmp_path: Path) -> AsyncIterator[AcpProcess]:
    process = AcpProcess(tmp_path, model)
    try:
        yield process
    finally:
        await process.kill()


async def handshake(conn: Any) -> Any:
    return await conn.initialize(
        protocol_version=1,
        client_capabilities=ClientCapabilities(),
        client_info=Implementation(name="boba-tests", version="0"),
    )


class Steps:
    """Шаги сценария, общие для команды и бинарника."""

    @staticmethod
    async def tool_turn_streams_updates(
        conn: Any, client: RecordingClient, session_id: str, frames: Frames
    ) -> None:
        response = await conn.prompt(
            session_id=session_id,
            prompt=[text_block(call({"name": "add", "arguments": {"a": 1, "b": 2}}))],
        )
        assert response.stop_reason == StopReasonName.END_TURN.value

        starts = client.starts()
        assert len(starts) == 1
        start = starts[0]
        assert start.title.startswith("add ")
        assert start.kind == ToolKindName.READ.value
        assert start.status == "pending"
        assert isinstance(start.raw_input, dict)
        assert start.raw_input["a"] == 1
        assert start.raw_input["b"] == 2
        assert client.permissions == []

        progress = client.progress_of(start.tool_call_id)
        assert [p.status for p in progress] == ["in_progress", "completed"]
        done = progress[-1]
        assert result_text(done) == "3"
        assert isinstance(done.raw_output, dict)

        chunks = client.chunks()
        assert chunks
        assert all(chunk.message_id for chunk in chunks)

        request_id = frames.request_id("session/prompt", -1)
        answered_at = frames.position_of_response(request_id)
        before = frames.incoming_notifications(answered_at)
        assert [m["method"] for m in before].count("session/update") >= 4
        assert all(
            m["method"] != "session/update" for m in frames.incoming_after(answered_at)
        )

    @staticmethod
    async def permission_is_asked_and_remembered(
        conn: Any, client: RecordingClient, session_id: str
    ) -> None:
        client.choice = PermissionChoice.ALLOW_ALWAYS
        response = await conn.prompt(
            session_id=session_id,
            prompt=[text_block(call({"name": "shout", "arguments": {"text": "hi"}}))],
        )
        assert response.stop_reason == StopReasonName.END_TURN.value
        assert len(client.permissions) == 1
        asked = client.permissions[0]
        assert asked.title is not None
        assert asked.title.startswith("shout ")
        assert asked.kind == ToolKindName.EDIT.value
        assert asked.status == "pending"
        assert [o.kind for o in client.options[0]] == [
            "allow_once",
            "allow_always",
            "reject_once",
        ]
        done = client.progress_of(asked.tool_call_id)[-1]
        assert done.status == "completed"
        assert done.content is not None
        assert done.content[0].content.text == "HI"

        response = await conn.prompt(
            session_id=session_id,
            prompt=[
                text_block(call({"name": "shout", "arguments": {"text": "again"}}))
            ],
        )
        assert response.stop_reason == StopReasonName.END_TURN.value
        assert len(client.permissions) == 1


class TestHandshake:
    async def test_initialize_reports_the_agent_and_its_capabilities(
        self, boba: AcpProcess
    ) -> None:
        client = RecordingClient()
        conn = await boba.start(client)
        try:
            response = await handshake(conn)

            assert response.protocol_version == 1
            assert response.agent_info is not None
            assert response.agent_info.name == "boba"
            assert response.agent_info.version
            capabilities = response.agent_capabilities
            assert capabilities is not None
            assert capabilities.load_session is True
            assert capabilities.mcp_capabilities is not None
            assert capabilities.mcp_capabilities.http is True
            assert capabilities.mcp_capabilities.sse is False
            assert capabilities.prompt_capabilities is not None
            assert capabilities.prompt_capabilities.image is False
            assert capabilities.session_capabilities is not None
            assert capabilities.session_capabilities.fork is not None
            assert response.auth_methods == []

            with pytest.raises(RequestError) as refused:
                await conn.authenticate(method_id="none")

            assert refused.value.code == METHOD_NOT_FOUND
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        boba.assert_stdout_is_frames_only()


class TestConversation:
    async def test_tool_turn_permission_and_stdout_discipline(
        self, boba: AcpProcess, mcp: McpStand, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        conn = await boba.start(client, None, "--record-requests")
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[mcp.server()]
            )
            session_id = session.session_id
            assert session_id

            await Steps.tool_turn_streams_updates(conn, client, session_id, boba.frames)
            await Steps.permission_is_asked_and_remembered(conn, client, session_id)
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        boba.assert_stdout_is_frames_only()
        assert any("INFO" in line for line in boba.stderr)

        records = boba.journal(session_id)
        types = [r["type"] for r in records]
        assert "api-request" in types
        assert "permission-rule" in types
        journal = json.dumps(records)
        assert API_KEY not in journal
        assert API_KEY not in "\n".join(boba.raw_stdout)
        assert API_KEY not in "\n".join(boba.stderr)

    async def test_secrets_stay_in_the_settings_file(
        self, boba: AcpProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        conn = await boba.start(client)
        try:
            await handshake(conn)
            pid = boba.process.pid
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            environ = Path(f"/proc/{pid}/environ").read_bytes()

            assert API_KEY.encode() not in cmdline
            assert API_KEY.encode() not in environ
            assert str(boba.settings_path()).encode() in cmdline
            assert stat.S_IMODE(boba.settings_path().stat().st_mode) == OWNER_ONLY
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_rejected_permission_fails_the_call(
        self, boba: AcpProcess, mcp: McpStand, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        client.choice = PermissionChoice.REJECT_ONCE
        conn = await boba.start(client)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[mcp.server()]
            )
            response = await conn.prompt(
                session_id=session.session_id,
                prompt=[
                    text_block(call({"name": "shout", "arguments": {"text": "no"}}))
                ],
            )

            assert response.stop_reason == StopReasonName.END_TURN.value
            assert len(client.permissions) == 1
            progress = client.progress_of(client.permissions[0].tool_call_id)
            assert [p.status for p in progress] == ["failed"]
            assert "reject" in result_text(progress[0]).lower()
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_progress_of_a_call_travels_in_meta(
        self, boba: AcpProcess, mcp: McpStand, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        conn = await boba.start(client)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[mcp.server()]
            )
            response = await conn.prompt(
                session_id=session.session_id,
                prompt=[
                    text_block(call({"name": "progress", "arguments": {"steps": 3}}))
                ],
            )

            assert response.stop_reason == StopReasonName.END_TURN.value
            start = client.starts()[0]
            reports: list[dict[str, Any]] = []
            for update in client.progress_of(start.tool_call_id):
                if update.field_meta is None:
                    continue

                reports.append(update.field_meta[MetaKey.PROGRESS.value])

            assert [r["progress"] for r in reports] == [1, 2, 3]
            assert reports[-1]["total"] == 3
            assert reports[-1]["message"] == "step 3 of 3"
            assert reports[-1]["elapsed_sec"] >= 0
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_resource_link_reaches_the_model_as_a_markdown_link(
        self, boba: AcpProcess, model: FakeModel, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        conn = await boba.start(client)
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            response = await conn.prompt(
                session_id=session.session_id,
                prompt=[
                    text_block(f"{ScenarioName.ANSWER.value} look at "),
                    resource_link_block("main.py", "file:///work/main.py"),
                ],
            )

            assert response.stop_reason == StopReasonName.END_TURN.value
            sent = model.app.requests[0]["messages"][-1]["content"]
            assert "[@main.py](file:///work/main.py)" in sent
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0


class TestQueueAndCancel:
    async def test_second_prompt_waits_for_its_own_turn(
        self, boba: AcpProcess, mcp: McpStand, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await boba.start(client, document)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[mcp.server()]
            )
            session_id = session.session_id
            first, second = await asyncio.gather(
                conn.prompt(
                    session_id=session_id,
                    prompt=[
                        text_block(
                            call({"name": "sleep_for", "arguments": {"seconds": 1.0}})
                        )
                    ],
                ),
                conn.prompt(
                    session_id=session_id,
                    prompt=[text_block(ScenarioName.ANSWER.value)],
                ),
            )

            assert first.stop_reason == StopReasonName.END_TURN.value
            assert second.stop_reason == StopReasonName.END_TURN.value
            first_id = boba.frames.request_id("session/prompt", 0)
            second_id = boba.frames.request_id("session/prompt", 1)
            assert boba.frames.position_of_response(
                first_id
            ) < boba.frames.position_of_response(second_id)
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        records = boba.journal(session_id)
        spoken = [
            r["message"]["content"]
            for r in records
            if r["type"] == "user" and isinstance(r["message"]["content"], str)
        ]
        assert len(spoken) == 2
        assert ScenarioName.ANSWER.value in spoken[1]

    async def test_cancel_stops_the_running_tool(
        self, boba: AcpProcess, mcp: McpStand, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await boba.start(client, document)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[mcp.server()]
            )
            prompting = asyncio.create_task(
                conn.prompt(
                    session_id=session.session_id,
                    prompt=[
                        text_block(
                            call({"name": "sleep_for", "arguments": {"seconds": 30}})
                        )
                    ],
                )
            )
            while not client.starts():
                await asyncio.sleep(0.05)

            await conn.cancel(session_id=session.session_id)
            response = await asyncio.wait_for(prompting, READ_TIMEOUT_SEC)

            assert response.stop_reason == StopReasonName.CANCELLED.value
            start = client.starts()[0]
            assert client.progress_of(start.tool_call_id)[-1].status == "failed"
            request_id = boba.frames.request_id("session/prompt", -1)
            answered_at = boba.frames.position_of_response(request_id)
            assert all(
                m["method"] != "session/update"
                for m in boba.frames.incoming_after(answered_at)
            )
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_cancel_withdraws_a_pending_permission_question(
        self, boba: AcpProcess, mcp: McpStand, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        client.hold = asyncio.Event()
        conn = await boba.start(client)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[mcp.server()]
            )
            prompting = asyncio.create_task(
                conn.prompt(
                    session_id=session.session_id,
                    prompt=[
                        text_block(call({"name": "shout", "arguments": {"text": "x"}}))
                    ],
                )
            )
            asked = await client.next_permission(0)
            await conn.cancel(session_id=session.session_id)
            response = await asyncio.wait_for(prompting, READ_TIMEOUT_SEC)

            assert response.stop_reason == StopReasonName.CANCELLED.value
            assert client.progress_of(asked.tool_call_id)[-1].status == "failed"

            client.hold.set()
            await asyncio.sleep(0.2)
            again = await conn.prompt(
                session_id=session.session_id,
                prompt=[text_block(ScenarioName.ANSWER.value)],
            )
            assert again.stop_reason == StopReasonName.END_TURN.value
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_cancel_request_notification_cancels_the_prompt(
        self, boba: AcpProcess, mcp: McpStand, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        boba.write_settings(document)
        await boba.spawn()
        conn = boba.raw_connection(client)
        try:
            await conn.send_request(
                "initialize", {"protocolVersion": 1, "clientCapabilities": {}}
            )
            session = await conn.send_request(
                "session/new",
                {
                    "cwd": str(tmp_path),
                    "mcpServers": [mcp.server().model_dump(mode="json", by_alias=True)],
                },
            )
            session_id = session["sessionId"]
            prompting = asyncio.create_task(
                conn.send_request(
                    "session/prompt",
                    {
                        "sessionId": session_id,
                        "prompt": [
                            {
                                "type": "text",
                                "text": call(
                                    {"name": "sleep_for", "arguments": {"seconds": 30}}
                                ),
                            }
                        ],
                    },
                )
            )
            while not client.starts():
                await asyncio.sleep(0.05)

            request_id = boba.frames.request_id("session/prompt", -1)
            await conn.send_notification("$/cancel_request", {"requestId": request_id})
            response = await asyncio.wait_for(prompting, READ_TIMEOUT_SEC)

            assert response["stopReason"] == StopReasonName.CANCELLED.value
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0


class TestRefusals:
    async def test_foreign_blocks_sessions_and_servers_are_refused(
        self, boba: AcpProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        conn = await boba.start(client)
        try:
            await handshake(conn)
            with pytest.raises(RequestError) as relative:
                await conn.new_session(cwd="relative/dir", mcp_servers=[])

            assert relative.value.code == INVALID_PARAMS

            with pytest.raises(RequestError) as sse:
                await conn.new_session(
                    cwd=str(tmp_path),
                    mcp_servers=[
                        SseMcpServer(
                            type="sse",
                            name="old",
                            url="http://127.0.0.1:9/sse",
                            headers=[],
                        )
                    ],
                )

            assert sse.value.code == INVALID_PARAMS

            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            with pytest.raises(RequestError) as image:
                await conn.prompt(
                    session_id=session.session_id,
                    prompt=[
                        ImageContentBlock(
                            type="image", data="AAAA", mime_type="image/png"
                        )
                    ],
                )

            assert image.value.code == INVALID_PARAMS

            with pytest.raises(RequestError) as unknown:
                await conn.prompt(
                    session_id="no-such-session", prompt=[text_block("hello")]
                )

            assert unknown.value.code == RESOURCE_NOT_FOUND
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_unreachable_mcp_server_fails_the_session(
        self, boba: AcpProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        conn = await boba.start(client)
        try:
            await handshake(conn)
            dead = HttpMcpServer(
                type="http",
                name="dead",
                url=f"http://127.0.0.1:{free_port()}/mcp",
                headers=[],
            )
            with pytest.raises(RequestError) as failed:
                await conn.new_session(cwd=str(tmp_path), mcp_servers=[dead])

            assert failed.value.code == INTERNAL_ERROR
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_model_failure_is_a_json_rpc_error(
        self, boba: AcpProcess, model: FakeModel, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        document = boba.settings_document()
        document["limits"] = {"apiRetries": 0}
        conn = await boba.start(client, document)
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            broken = f"{ScenarioName.ANSWER.value} {FailureName.STATUS_500.value}"
            with pytest.raises(RequestError) as failed:
                await conn.prompt(
                    session_id=session.session_id, prompt=[text_block(broken)]
                )

            assert failed.value.code == INTERNAL_ERROR
            assert isinstance(failed.value.data, dict)
            assert failed.value.data["kind"] == "api_error"
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0


class TestLifecycle:
    async def test_sigterm_exits_zero_with_a_flushed_journal(
        self, boba: AcpProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        conn = await boba.start(client)
        await handshake(conn)
        session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        response = await conn.prompt(
            session_id=session.session_id,
            prompt=[text_block(ScenarioName.ANSWER.value)],
        )
        assert response.stop_reason == StopReasonName.END_TURN.value

        boba.process.send_signal(signal.SIGTERM)
        assert await boba.wait() == 0
        await conn.close()

        records = boba.journal(session.session_id)
        assert [r["type"] for r in records][-1] == "assistant"

    async def test_end_of_input_closes_the_sessions(
        self, boba: AcpProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        conn = await boba.start(client)
        await handshake(conn)
        session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        await conn.close()
        await boba.close_stdin()

        assert await boba.wait() == 0
        assert (tmp_path / "history" / f"{session.session_id}.jsonl").exists()
        boba.assert_stdout_is_frames_only()

    async def test_bad_settings_fail_before_the_protocol(
        self, boba: AcpProcess, tmp_path: Path
    ) -> None:
        document = boba.settings_document()
        boba.write_settings(document)
        boba.settings_path().chmod(0o644)
        await boba.spawn()

        assert await boba.wait() == 1
        assert boba.raw_stdout == []
        assert any("chmod 600" in line for line in boba.stderr)


class TestHistory:
    """Сессии и история: второй процесс над тем же журналом."""

    PROMPTS: ClassVar[tuple[str, ...]] = (
        call({"name": "add", "arguments": {"a": 2, "b": 3}}),
        ScenarioName.THINKING_ANSWER.value,
    )

    async def conversation(
        self, boba: AcpProcess, mcp: McpStand, cwd: Path
    ) -> tuple[str, list[Any]]:
        """Первый процесс: сессия с двумя ходами; возвращает её id и живые
        уведомления; процесс завершён."""
        client = RecordingClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await boba.start(client, document)
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(cwd), mcp_servers=[mcp.server()])
            for prompt in self.PROMPTS:
                response = await conn.prompt(
                    session_id=session.session_id, prompt=[text_block(prompt)]
                )
                assert response.stop_reason == StopReasonName.END_TURN.value
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

        return session.session_id, client.updates_of(session.session_id)

    async def test_load_replays_the_conversation_like_the_live_turn(
        self, boba: AcpProcess, model: FakeModel, mcp: McpStand, tmp_path: Path
    ) -> None:
        session_id, live = await self.conversation(boba, mcp, tmp_path)

        second = AcpProcess(tmp_path, model)
        client = RecordingClient()
        document = second.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await second.start(client, document)
        try:
            await handshake(conn)
            await conn.load_session(
                cwd=str(tmp_path), session_id=session_id, mcp_servers=[mcp.server()]
            )
            replayed = client.updates_of(session_id)

            view = FeedView()
            live_view = view.render(live)
            replay_view = view.render(replayed)
            assert view.user_texts(live_view) == []
            assert view.user_texts(replay_view) == list(self.PROMPTS)
            assert view.without_user(replay_view) == view.without_user(live_view)
            assert len(view.without_user(replay_view)) >= 4

            request_id = second.frames.request_id("session/load")
            answered_at = second.frames.position_of_response(request_id)
            before = second.frames.incoming_notifications(answered_at)
            assert len(before) == len(replayed)
            assert all(
                m["method"] != "session/update"
                for m in second.frames.incoming_after(answered_at)
            )

            response = await conn.prompt(
                session_id=session_id, prompt=[text_block(ScenarioName.ANSWER.value)]
            )
            assert response.stop_reason == StopReasonName.END_TURN.value
        finally:
            await conn.close()
            await second.close_stdin()
            await second.kill()

        human = [
            r["message"]["content"]
            for r in second.journal(session_id)
            if r["type"] == "user" and r.get("origin", {}).get("kind") == "human"
        ]
        assert human == [*self.PROMPTS, ScenarioName.ANSWER.value]

    async def test_resume_restores_without_replay(
        self, boba: AcpProcess, model: FakeModel, mcp: McpStand, tmp_path: Path
    ) -> None:
        session_id, _ = await self.conversation(boba, mcp, tmp_path)

        second = AcpProcess(tmp_path, model)
        client = RecordingClient()
        conn = await second.start(client)
        try:
            await handshake(conn)
            await conn.resume_session(
                cwd=str(tmp_path), session_id=session_id, mcp_servers=[mcp.server()]
            )
            assert client.updates == []

            response = await conn.prompt(
                session_id=session_id, prompt=[text_block(ScenarioName.ANSWER.value)]
            )
            assert response.stop_reason == StopReasonName.END_TURN.value
            assert client.chunks()
        finally:
            await conn.close()
            await second.close_stdin()
            await second.kill()

    async def test_unknown_session_is_resource_not_found(
        self, boba: AcpProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        conn = await boba.start(client)
        try:
            await handshake(conn)
            for request in (
                conn.load_session(
                    cwd=str(tmp_path), session_id="no-such", mcp_servers=[]
                ),
                conn.resume_session(
                    cwd=str(tmp_path), session_id="no-such", mcp_servers=[]
                ),
                conn.close_session(session_id="no-such"),
                conn.load_session(
                    cwd=str(tmp_path), session_id="bad/id", mcp_servers=[]
                ),
            ):
                with pytest.raises(RequestError) as unknown:
                    await request

                assert unknown.value.code == RESOURCE_NOT_FOUND

            with pytest.raises(RequestError) as relative:
                await conn.load_session(cwd="relative", session_id="x", mcp_servers=[])

            assert relative.value.code == INVALID_PARAMS
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_close_frees_the_session_and_load_reopens_it(
        self, boba: AcpProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        conn = await boba.start(client)
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            session_id = session.session_id
            response = await conn.prompt(
                session_id=session_id, prompt=[text_block(ScenarioName.ANSWER.value)]
            )
            assert response.stop_reason == StopReasonName.END_TURN.value

            await conn.close_session(session_id=session_id)
            with pytest.raises(RequestError) as gone:
                await conn.prompt(session_id=session_id, prompt=[text_block("hi")])

            assert gone.value.code == RESOURCE_NOT_FOUND
            with pytest.raises(RequestError) as twice:
                await conn.close_session(session_id=session_id)

            assert twice.value.code == RESOURCE_NOT_FOUND

            client.updates.clear()
            await conn.load_session(
                cwd=str(tmp_path), session_id=session_id, mcp_servers=[]
            )
            view = FeedView().render(client.updates_of(session_id))
            assert [item[0] for item in view] == ["user", "agent"]
            assert view[0][2] == ScenarioName.ANSWER.value

            again = await conn.prompt(
                session_id=session_id, prompt=[text_block(ScenarioName.ANSWER.value)]
            )
            assert again.stop_reason == StopReasonName.END_TURN.value
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0


class TestListAndDelete:
    PAGE: ClassVar[int] = 50

    @staticmethod
    async def all_pages(conn: Any, cwd: str) -> list[Any]:
        """Все страницы списка с фильтром по каталогу: фильтр отбирает из
        страницы, поэтому страница может быть короче предела."""
        found: list[Any] = []
        cursor: str | None = None
        while True:
            page = await conn.list_sessions(cwd=cwd, cursor=cursor)
            found.extend(page.sessions)
            cursor = page.next_cursor
            if cursor is None:
                return found

    async def test_list_pages_with_a_cursor_and_filters_by_cwd(
        self, boba: AcpProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        conn = await boba.start(client)
        quiet_cwd = tmp_path / "quiet"
        spoken_cwd = tmp_path / "spoken"
        try:
            await handshake(conn)
            created: list[str] = []
            for _ in range(self.PAGE + 2):
                session = await conn.new_session(cwd=str(quiet_cwd), mcp_servers=[])
                created.append(session.session_id)

            spoken = await conn.new_session(cwd=str(spoken_cwd), mcp_servers=[])
            question = f"{ScenarioName.ANSWER.value} what is the plan\nsecond line"
            await conn.prompt(
                session_id=spoken.session_id, prompt=[text_block(question)]
            )
            for session_id in [*created, spoken.session_id]:
                await conn.close_session(session_id=session_id)

            first = await conn.list_sessions()
            assert len(first.sessions) == self.PAGE
            assert first.next_cursor == first.sessions[-1].session_id
            second = await conn.list_sessions(cursor=first.next_cursor)
            assert len(second.sessions) == 3
            assert second.next_cursor is None

            listed = [s.session_id for s in [*first.sessions, *second.sessions]]
            assert listed == sorted(listed)
            assert set(listed) == {*created, spoken.session_id}
            assert all(s.updated_at for s in first.sessions)

            by_spoken = await conn.list_sessions(cwd=str(spoken_cwd))
            assert [s.session_id for s in by_spoken.sessions] == [spoken.session_id]
            assert (
                by_spoken.sessions[0].title
                == f"{ScenarioName.ANSWER.value} what is the plan"
            )
            assert by_spoken.sessions[0].cwd == str(spoken_cwd)

            by_process = await self.all_pages(conn, str(tmp_path))
            assert {s.session_id for s in by_process} == set(created)
            assert all(s.title is None for s in by_process)

            empty = await conn.list_sessions(cwd=str(tmp_path / "nowhere"))
            assert empty.sessions == []

            with pytest.raises(RequestError) as relative:
                await conn.list_sessions(cwd="relative")

            assert relative.value.code == INVALID_PARAMS
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_delete_removes_the_session_and_repeats_silently(
        self, boba: AcpProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        boba.write_settings(boba.settings_document())
        await boba.spawn()
        conn = boba.raw_connection(client)
        try:
            await conn.send_request(
                "initialize", {"protocolVersion": 1, "clientCapabilities": {}}
            )
            session = await conn.send_request(
                "session/new", {"cwd": str(tmp_path), "mcpServers": []}
            )
            session_id = session["sessionId"]
            await conn.send_request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": ScenarioName.ANSWER.value}],
                },
            )
            journal = tmp_path / "history" / f"{session_id}.jsonl"
            assert journal.exists()

            assert (
                await conn.send_request("session/delete", {"sessionId": session_id})
                == {}
            )
            assert not journal.exists()
            listed = await conn.send_request("session/list", {})
            assert listed["sessions"] == []
            with pytest.raises(RequestError) as gone:
                await conn.send_request(
                    "session/prompt",
                    {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": "x"}],
                    },
                )

            assert gone.value.code == RESOURCE_NOT_FOUND

            assert (
                await conn.send_request("session/delete", {"sessionId": session_id})
                == {}
            )
            assert (
                await conn.send_request("session/delete", {"sessionId": "never"}) == {}
            )
            assert (
                await conn.send_request("session/delete", {"sessionId": "bad/id"}) == {}
            )
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0


def update_kinds(frames: Frames, request_method: str) -> list[str]:
    """Виды `session/update`, пришедших до ответа на последний запрос метода."""
    request_id = frames.request_id(request_method, -1)
    answered_at = frames.position_of_response(request_id)
    kinds: list[str] = []
    for message in frames.incoming_notifications(answered_at):
        if message["method"] != "session/update":
            continue

        kinds.append(message["params"]["update"]["sessionUpdate"])

    assert all(
        m["method"] != "session/update" for m in frames.incoming_after(answered_at)
    )

    return kinds


def option_of(response: Any, option_id: ConfigOptionId) -> SessionConfigOptionSelect:
    """Настройка `select` из ответа или уведомления с полным состоянием."""
    assert response.config_options is not None
    for option in response.config_options:
        if option.id == option_id.value:
            assert isinstance(option, SessionConfigOptionSelect)
            return option

    msg = f"configOption {option_id.value!r} is missing"
    raise AssertionError(msg)


def option_values(option: SessionConfigOptionSelect) -> list[str]:
    values: list[str] = []
    for item in option.options:
        assert not isinstance(item, SessionConfigSelectGroup)
        values.append(item.value)

    return values


class TestSessionSettings:
    """Режимы и настройки сессии: ответы `session/new|load|resume`,
    `set_mode` и `set_config_option` с уведомлениями, режим `plan` в деле,
    смена модели между ходами и отказ при идущем ходе."""

    MODES: ClassVar[list[str]] = ["default", "acceptEdits", "plan", "dontAsk"]

    async def test_new_load_and_resume_report_modes_and_config_options(
        self, boba: AcpProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        conn = await boba.start(client)
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            session_id = session.session_id
            self.check_state(session, "default", "fake")
            assert session.modes is not None
            assert all(m.name and m.description for m in session.modes.available_modes)

            await conn.prompt(
                session_id=session_id, prompt=[text_block(ScenarioName.ANSWER.value)]
            )
            await conn.close_session(session_id=session_id)
            loaded = await conn.load_session(
                cwd=str(tmp_path), session_id=session_id, mcp_servers=[]
            )
            self.check_state(loaded, "default", "fake")

            await conn.close_session(session_id=session_id)
            client.updates.clear()
            resumed = await conn.resume_session(
                cwd=str(tmp_path), session_id=session_id, mcp_servers=[]
            )
            self.check_state(resumed, "default", "fake")
            assert client.updates == []
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_bypass_is_offered_only_with_the_right(
        self, boba: AcpProcess, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await boba.start(client, document)
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            assert session.modes is not None
            assert session.modes.current_mode_id == "bypassPermissions"
            assert [m.id for m in session.modes.available_modes] == [
                *self.MODES,
                "bypassPermissions",
            ]
            mode = option_of(session, ConfigOptionId.MODE)
            assert mode.current_value == "bypassPermissions"
            assert option_values(mode) == [*self.MODES, "bypassPermissions"]
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    async def test_set_mode_changes_permissions_and_notifies(
        self, boba: AcpProcess, mcp: McpStand, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        conn = await boba.start(client)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[mcp.server()]
            )
            session_id = session.session_id
            await conn.set_session_mode(session_id=session_id, mode_id="plan")
            assert [m.current_mode_id for m in client.of(CurrentModeUpdate)] == ["plan"]
            announced = client.of(ConfigOptionUpdate)
            assert len(announced) == 1
            assert option_of(announced[0], ConfigOptionId.MODE).current_value == "plan"

            response = await conn.prompt(
                session_id=session_id,
                prompt=[
                    text_block(call({"name": "shout", "arguments": {"text": "x"}}))
                ],
            )
            assert response.stop_reason == StopReasonName.END_TURN.value
            assert client.permissions == []
            progress = client.progress_of(client.starts()[0].tool_call_id)
            assert [p.status for p in progress] == ["failed"]
            assert "plan" in result_text(progress[0]).lower()

            with pytest.raises(RequestError) as bypass:
                await conn.set_config_option(
                    session_id=session_id,
                    config_id=ConfigOptionId.MODE.value,
                    value="bypassPermissions",
                )

            assert bypass.value.code == INVALID_PARAMS
            with pytest.raises(RequestError) as unknown:
                await conn.set_session_mode(session_id=session_id, mode_id="turbo")

            assert unknown.value.code == INVALID_PARAMS

            changed = await conn.set_config_option(
                session_id=session_id,
                config_id=ConfigOptionId.MODE.value,
                value="default",
            )
            assert option_of(changed, ConfigOptionId.MODE).current_value == "default"
            assert option_of(changed, ConfigOptionId.MODEL).current_value == "fake"
            assert [m.current_mode_id for m in client.of(CurrentModeUpdate)] == [
                "plan",
                "default",
            ]
            assert len(client.of(ConfigOptionUpdate)) == 2

            response = await conn.prompt(
                session_id=session_id,
                prompt=[
                    text_block(call({"name": "shout", "arguments": {"text": "y"}}))
                ],
            )
            assert response.stop_reason == StopReasonName.END_TURN.value
            assert len(client.permissions) == 1
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        modes = [
            r["permissionMode"]
            for r in boba.journal(session_id)
            if r["type"] == "permission-mode"
        ]
        assert modes == ["default", "plan", "default"]

    async def test_model_switch_changes_the_profile_of_the_next_request(
        self, boba: AcpProcess, model: FakeModel, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        document = boba.settings_document()
        document["models"]["fake"]["sampling"] = {"temperature": 0.1}
        document["models"]["second"] = boba.profile_document(
            "second-model", sampling={"temperature": 0.9}
        )
        conn = await boba.start(client, document, "--record-requests")
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            session_id = session.session_id
            assert option_values(option_of(session, ConfigOptionId.MODEL)) == [
                "fake",
                "second",
            ]
            await conn.prompt(
                session_id=session_id, prompt=[text_block(ScenarioName.ANSWER.value)]
            )

            changed = await conn.set_config_option(
                session_id=session_id,
                config_id=ConfigOptionId.MODEL.value,
                value="second",
            )
            assert option_of(changed, ConfigOptionId.MODEL).current_value == "second"
            announced = client.of(ConfigOptionUpdate)
            assert len(announced) == 1
            assert option_of(announced[0], ConfigOptionId.MODEL).current_value == (
                "second"
            )
            assert client.of(CurrentModeUpdate) == []

            response = await conn.prompt(
                session_id=session_id, prompt=[text_block(ScenarioName.ANSWER.value)]
            )
            assert response.stop_reason == StopReasonName.END_TURN.value
            assert [r["model"] for r in model.app.requests] == [
                "fake-model",
                "second-model",
            ]

            for config_id, value in (
                (ConfigOptionId.MODEL.value, "third"),
                ("temperature", "0.5"),
                (ConfigOptionId.MODEL.value, True),
            ):
                with pytest.raises(RequestError) as refused:
                    await conn.set_config_option(
                        session_id=session_id, config_id=config_id, value=value
                    )

                assert refused.value.code == INVALID_PARAMS
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0
        records = boba.journal(session_id)
        temperatures = [
            r["params"]["sampling"]["temperature"]
            for r in records
            if r["type"] == "api-request"
        ]
        assert temperatures == [0.1, 0.9]
        announced_models = [
            r["attachment"]["identity"]["modelId"]
            for r in records
            if r["type"] == "attachment" and r["attachment"]["type"] == "model"
        ]
        assert announced_models == ["fake-model", "second-model"]

    async def test_model_switch_waits_for_an_idle_session(
        self, boba: AcpProcess, mcp: McpStand, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        document["models"]["second"] = boba.profile_document("second-model")
        conn = await boba.start(client, document)
        try:
            await handshake(conn)
            session = await conn.new_session(
                cwd=str(tmp_path), mcp_servers=[mcp.server()]
            )
            session_id = session.session_id
            prompting = asyncio.create_task(
                conn.prompt(
                    session_id=session_id,
                    prompt=[
                        text_block(
                            call({"name": "sleep_for", "arguments": {"seconds": 2.0}})
                        )
                    ],
                )
            )
            while not client.starts():
                await asyncio.sleep(0.05)

            with pytest.raises(RequestError) as busy:
                await conn.set_config_option(
                    session_id=session_id,
                    config_id=ConfigOptionId.MODEL.value,
                    value="second",
                )

            assert busy.value.code == INTERNAL_ERROR
            assert isinstance(busy.value.data, dict)
            assert "busy" in busy.value.data["details"]

            await conn.set_session_mode(session_id=session_id, mode_id="default")
            assert [m.current_mode_id for m in client.of(CurrentModeUpdate)] == [
                "default"
            ]

            response = await asyncio.wait_for(prompting, READ_TIMEOUT_SEC)
            assert response.stop_reason == StopReasonName.END_TURN.value
            changed = await conn.set_config_option(
                session_id=session_id,
                config_id=ConfigOptionId.MODEL.value,
                value="second",
            )
            assert option_of(changed, ConfigOptionId.MODEL).current_value == "second"
            assert option_of(changed, ConfigOptionId.MODE).current_value == "default"
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

    def check_state(self, response: Any, mode: str, model: str) -> None:
        assert response.modes is not None
        assert response.modes.current_mode_id == mode
        assert [m.id for m in response.modes.available_modes] == self.MODES
        assert response.config_options is not None
        assert [o.id for o in response.config_options] == ["mode", "model"]
        mode_option = option_of(response, ConfigOptionId.MODE)
        assert mode_option.category == "mode"
        assert mode_option.current_value == mode
        assert option_values(mode_option) == self.MODES
        model_option = option_of(response, ConfigOptionId.MODEL)
        assert model_option.category == "model"
        assert model_option.current_value == model
        assert option_values(model_option) == [model]


class TestServiceUpdates:
    """`usage_update` после каждого хода и после сжатия, `session_info_update`
    с заголовком после первого хода и при `load`; все — до ответа на запрос."""

    SIZE: ClassVar[int] = 32000 - 4096
    TITLE: ClassVar[str] = f"{ScenarioName.ANSWER.value} what is the plan"

    async def test_usage_and_title_follow_the_turn_and_the_load(
        self, boba: AcpProcess, model: FakeModel, tmp_path: Path
    ) -> None:
        client = RecordingClient()
        conn = await boba.start(client)
        try:
            await handshake(conn)
            session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
            session_id = session.session_id
            await conn.prompt(
                session_id=session_id,
                prompt=[text_block(f"{self.TITLE}\nsecond line")],
            )
            usage = client.of(UsageUpdate)
            assert len(usage) == 1
            assert usage[0].size == self.SIZE
            assert 0 < usage[0].used < self.SIZE
            assert [t.title for t in client.of(SessionInfoUpdate)] == [self.TITLE]
            kinds = update_kinds(boba.frames, "session/prompt")
            assert kinds[-2:] == ["usage_update", "session_info_update"]

            await conn.prompt(
                session_id=session_id, prompt=[text_block(ScenarioName.ANSWER.value)]
            )
            assert len(client.of(UsageUpdate)) == 2
            assert len(client.of(SessionInfoUpdate)) == 1
            assert update_kinds(boba.frames, "session/prompt")[-1] == "usage_update"
        finally:
            await conn.close()
            await boba.close_stdin()

        assert await boba.wait() == 0

        second = AcpProcess(tmp_path, model)
        loader = RecordingClient()
        conn = await second.start(loader)
        try:
            await handshake(conn)
            await conn.load_session(
                cwd=str(tmp_path), session_id=session_id, mcp_servers=[]
            )
            assert [t.title for t in loader.of(SessionInfoUpdate)] == [self.TITLE]
            assert len(loader.of(UsageUpdate)) == 1
            kinds = update_kinds(second.frames, "session/load")
            assert kinds[-2:] == ["usage_update", "session_info_update"]
            assert kinds[0] == "user_message_chunk"
        finally:
            await conn.close()
            await second.close_stdin()
            await second.kill()

    async def test_compaction_reports_usage_only(
        self, mcp: McpStand, tmp_path: Path
    ) -> None:
        counting = FakeModel(estimate_usage=True)
        await counting.start()
        boba = AcpProcess(tmp_path, counting)
        client = RecordingClient()
        document = boba.settings_document(mode="bypassPermissions", allow_bypass=True)
        document["models"]["fake"] = boba.profile_document(
            "fake-model", context_window=8000, max_output_tokens=512, chars_per_token=4
        )
        try:
            conn = await boba.start(client, document)
            try:
                await handshake(conn)
                session = await conn.new_session(
                    cwd=str(tmp_path), mcp_servers=[mcp.server()]
                )
                session_id = session.session_id
                first = await conn.prompt(
                    session_id=session_id,
                    prompt=[
                        text_block(
                            call({"name": "large", "arguments": {"size": 24000}})
                        )
                    ],
                )
                assert first.stop_reason == StopReasonName.END_TURN.value
                assert len(client.of(UsageUpdate)) == 1

                second = await conn.prompt(
                    session_id=session_id,
                    prompt=[text_block(ScenarioName.ANSWER.value)],
                )
                assert second.stop_reason == StopReasonName.END_TURN.value
                usage = client.of(UsageUpdate)
                assert len(usage) == 3
                assert usage[1].used < usage[0].used
                assert all(u.size == 8000 - 512 for u in usage)
                kinds = update_kinds(boba.frames, "session/prompt")
                assert kinds.count("usage_update") == 3
                assert kinds[-1] == "usage_update"
            finally:
                await conn.close()
                await boba.close_stdin()

            assert await boba.wait() == 0
        finally:
            await boba.kill()
            await counting.stop()

        records = boba.journal(session_id)
        boundaries = [
            r
            for r in records
            if r["type"] == "system" and r.get("subtype") == "compact_boundary"
        ]
        assert len(boundaries) == 1
        assert any(r["type"] == "user" and r.get("isCompactSummary") for r in records)


class TestFork:
    """Ветвление сессии хранилища: срез по uuid вопроса, копия целиком,
    пустая ветка, отказы."""

    PROMPTS: ClassVar[tuple[str, ...]] = TestHistory.PROMPTS

    @staticmethod
    def questions(rendered: Sequence[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        return [item for item in rendered if item[0] == "user"]

    @staticmethod
    def human(journal: Sequence[Document]) -> list[str]:
        found: list[str] = []
        for record in journal:
            if record["type"] != "user":
                continue

            if record.get("origin", {}).get("kind") != "human":
                continue

            found.append(record["message"]["content"])

        return found

    async def test_fork_before_the_second_question_keeps_only_the_first_turn(
        self, boba: AcpProcess, model: FakeModel, mcp: McpStand, tmp_path: Path
    ) -> None:
        session_id, _ = await TestHistory().conversation(boba, mcp, tmp_path)
        source_before = boba.journal(session_id)

        second = AcpProcess(tmp_path, model)
        client = RecordingClient()
        document = second.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await second.start(client, document)
        try:
            await handshake(conn)
            await conn.load_session(
                cwd=str(tmp_path), session_id=session_id, mcp_servers=[mcp.server()]
            )
            view = FeedView()
            original = view.render(client.updates_of(session_id))
            questions = self.questions(original)
            assert [q[2] for q in questions] == list(self.PROMPTS)
            second_question = questions[1]

            forked = await conn.fork_session(
                session_id=session_id,
                cwd=str(tmp_path),
                mcp_servers=[mcp.server()],
                **{MetaKey.UP_TO_UUID.value: second_question[1]},
            )
            branch_id = forked.session_id
            assert branch_id != session_id
            assert client.updates_of(branch_id) == []
            assert forked.modes is not None
            assert forked.modes.current_mode_id == "bypassPermissions"
            assert option_of(forked, ConfigOptionId.MODE).current_value == (
                "bypassPermissions"
            )
            assert option_of(forked, ConfigOptionId.MODEL).current_value == "fake"

            await conn.load_session(
                cwd=str(tmp_path), session_id=branch_id, mcp_servers=[mcp.server()]
            )
            branch = view.render(client.updates_of(branch_id))
            assert branch == original[: original.index(second_question)]
            assert view.user_texts(branch) == [self.PROMPTS[0]]
            assert len(view.without_user(branch)) >= 3

            response = await conn.prompt(
                session_id=branch_id, prompt=[text_block(ScenarioName.ANSWER.value)]
            )
            assert response.stop_reason == StopReasonName.END_TURN.value

            again = await conn.prompt(
                session_id=session_id, prompt=[text_block(ScenarioName.ANSWER.value)]
            )
            assert again.stop_reason == StopReasonName.END_TURN.value
        finally:
            await conn.close()
            await second.close_stdin()
            assert await second.wait() == 0

        cut = [r.get("uuid") for r in source_before].index(second_question[1])
        prefix = [{**r, "sessionId": branch_id} for r in source_before[:cut]]
        branch_journal = second.journal(branch_id)
        assert branch_journal[:cut] == prefix
        assert [r["seq"] for r in branch_journal[:cut]] == list(range(1, cut + 1))
        assert branch_journal[cut]["seq"] == cut + 1
        assert self.human(branch_journal) == [
            self.PROMPTS[0],
            ScenarioName.ANSWER.value,
        ]

        source_after = second.journal(session_id)
        assert source_after[: len(source_before)] == source_before
        assert self.human(source_after) == [*self.PROMPTS, ScenarioName.ANSWER.value]

    async def test_fork_without_a_cut_copies_everything_after_a_flush(
        self, boba: AcpProcess, model: FakeModel, mcp: McpStand, tmp_path: Path
    ) -> None:
        session_id, _ = await TestHistory().conversation(boba, mcp, tmp_path)

        second = AcpProcess(tmp_path, model)
        client = RecordingClient()
        document = second.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await second.start(client, document)
        try:
            await handshake(conn)
            await conn.resume_session(
                cwd=str(tmp_path), session_id=session_id, mcp_servers=[mcp.server()]
            )
            response = await conn.prompt(
                session_id=session_id, prompt=[text_block(ScenarioName.ANSWER.value)]
            )
            assert response.stop_reason == StopReasonName.END_TURN.value

            forked = await conn.fork_session(
                session_id=session_id, cwd=str(tmp_path), mcp_servers=[mcp.server()]
            )
            branch_id = forked.session_id
            await conn.load_session(
                cwd=str(tmp_path), session_id=branch_id, mcp_servers=[mcp.server()]
            )
            branch = FeedView().render(client.updates_of(branch_id))
            assert FeedView().user_texts(branch) == [
                *self.PROMPTS,
                ScenarioName.ANSWER.value,
            ]
            await conn.close_session(session_id=session_id)
            await conn.close_session(session_id=branch_id)
        finally:
            await conn.close()
            await second.close_stdin()
            assert await second.wait() == 0

        source = second.journal(session_id)
        branch_journal = second.journal(branch_id)
        assert branch_journal == [{**r, "sessionId": branch_id} for r in source]

    async def test_fork_at_the_first_question_is_an_empty_session(
        self, boba: AcpProcess, model: FakeModel, mcp: McpStand, tmp_path: Path
    ) -> None:
        session_id, _ = await TestHistory().conversation(boba, mcp, tmp_path)

        second = AcpProcess(tmp_path, model)
        client = RecordingClient()
        document = second.settings_document(mode="bypassPermissions", allow_bypass=True)
        conn = await second.start(client, document)
        try:
            await handshake(conn)
            await conn.load_session(
                cwd=str(tmp_path), session_id=session_id, mcp_servers=[mcp.server()]
            )
            first_question = self.questions(
                FeedView().render(client.updates_of(session_id))
            )[0]

            forked = await conn.fork_session(
                session_id=session_id,
                cwd=str(tmp_path),
                mcp_servers=[],
                **{MetaKey.UP_TO_UUID.value: first_question[1]},
            )
            branch_id = forked.session_id
            await conn.load_session(
                cwd=str(tmp_path), session_id=branch_id, mcp_servers=[]
            )
            assert FeedView().render(client.updates_of(branch_id)) == []
            assert [type(u) for u in client.updates_of(branch_id)] == [UsageUpdate]

            response = await conn.prompt(
                session_id=branch_id, prompt=[text_block(ScenarioName.ANSWER.value)]
            )
            assert response.stop_reason == StopReasonName.END_TURN.value
        finally:
            await conn.close()
            await second.close_stdin()
            assert await second.wait() == 0

        branch_journal = second.journal(branch_id)
        assert branch_journal[0]["seq"] == 1
        assert self.human(branch_journal) == [ScenarioName.ANSWER.value]

    async def test_refusals(
        self, boba: AcpProcess, model: FakeModel, mcp: McpStand, tmp_path: Path
    ) -> None:
        session_id, _ = await TestHistory().conversation(boba, mcp, tmp_path)
        journals = sorted((tmp_path / "history").glob("*.jsonl"))

        second = AcpProcess(tmp_path, model)
        client = RecordingClient()
        conn = await second.start(client)
        try:
            await handshake(conn)
            for session in ("no-such", "bad/id"):
                with pytest.raises(RequestError) as unknown:
                    await conn.fork_session(
                        session_id=session, cwd=str(tmp_path), mcp_servers=[]
                    )

                assert unknown.value.code == RESOURCE_NOT_FOUND

            with pytest.raises(RequestError) as no_record:
                await conn.fork_session(
                    session_id=session_id,
                    cwd=str(tmp_path),
                    mcp_servers=[],
                    **{MetaKey.UP_TO_UUID.value: "nope"},
                )

            assert no_record.value.code == INVALID_PARAMS
            assert isinstance(no_record.value.data, dict)
            assert no_record.value.data["upToUuid"] == "nope"
            assert no_record.value.data["sessionId"] == session_id

            with pytest.raises(RequestError) as blank:
                await conn.fork_session(
                    session_id=session_id,
                    cwd=str(tmp_path),
                    mcp_servers=[],
                    **{MetaKey.UP_TO_UUID.value: ""},
                )

            assert blank.value.code == INVALID_PARAMS

            with pytest.raises(RequestError) as relative:
                await conn.fork_session(
                    session_id=session_id, cwd="relative", mcp_servers=[]
                )

            assert relative.value.code == INVALID_PARAMS

            dead = HttpMcpServer(
                type="http",
                name="dead",
                url=f"http://127.0.0.1:{free_port()}/mcp",
                headers=[],
            )
            with pytest.raises(RequestError) as unreachable:
                await conn.fork_session(
                    session_id=session_id, cwd=str(tmp_path), mcp_servers=[dead]
                )

            assert unreachable.value.code == INTERNAL_ERROR
        finally:
            await conn.close()
            await second.close_stdin()
            assert await second.wait() == 0

        assert sorted((tmp_path / "history").glob("*.jsonl")) == journals
