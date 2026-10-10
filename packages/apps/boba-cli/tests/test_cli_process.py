"""Процесс `boba` на стенде: строки JSON в стандартный ввод, события из
стандартного вывода. Сценарий критерия этапа 6: инструмент MCP, разрешение,
смена режима, отзыв сообщения, прерывание, возобновление; битые строки дают
событие error и не роняют процесс; SIGTERM и конец ввода завершают процесс с
кодом 0 и сброшенным журналом; возобновление несуществующей сессии — ошибка
и ненулевой код; `initialize` первой строкой — ещё один слой настроек;
`--record-requests` пишет api-request; в стандартный вывод не попадает
ничего, кроме строк протокола."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest
import uvicorn

from boba.stand.ui.fake_llm import FakeLlmApp, ScenarioName
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
Line = dict[str, Any]


def call(*specs: dict[str, object]) -> str:
    if len(specs) == 1:
        return f"{ScenarioName.CALL.value} {json.dumps(specs[0])}"

    return f"{ScenarioName.CALL.value} {json.dumps(list(specs))}"


def user(text: str, uuid: str | None = None, priority: str | None = None) -> Line:
    line: Line = {"type": "user", "message": {"role": "user", "content": text}}
    if uuid is not None:
        line["uuid"] = uuid

    if priority is not None:
        line["priority"] = priority

    return line


def command(request_id: str, subtype: str, **fields: object) -> Line:
    return {
        "type": "control_request",
        "request_id": request_id,
        "request": {"subtype": subtype, **fields},
    }


def allow(request_id: str, rules: Sequence[str] = ()) -> Line:
    response: Line = {"behavior": "allow"}
    if rules:
        response["updatedPermissions"] = [
            {"tool": tool, "behavior": "allow"} for tool in rules
        ]

    return {
        "type": "control_response",
        "response": {
            "request_id": request_id,
            "subtype": "success",
            "response": response,
        },
    }


def is_result(line: Line) -> bool:
    return line.get("type") == "result"


def is_question(line: Line) -> bool:
    return line.get("type") == "control_request"


def is_response(request_id: str) -> Callable[[Line], bool]:
    def check(line: Line) -> bool:
        if line.get("type") != "control_response":
            return False

        return line["response"]["request_id"] == request_id

    return check


def is_error(line: Line) -> bool:
    return line.get("type") == "system" and line.get("subtype") == "error"


class FakeModel:
    """FakeLlmApp под uvicorn в цикле теста."""

    def __init__(self) -> None:
        self.app = FakeLlmApp(token_delay_sec=0.0)
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


class BobaProcess:
    """Запущенный `boba`: строки туда, события оттуда, stderr в список.
    Команда по умолчанию — модуль из venv; бинарник передаётся явно."""

    MODULE: ClassVar[str] = "boba.cli.app"

    def __init__(
        self, root: Path, model: FakeModel, command: Sequence[str] | None = None
    ) -> None:
        if command is None:
            command = (sys.executable, "-m", self.MODULE)

        self._command = list(command)
        self._root = root
        self._model = model
        self._process: asyncio.subprocess.Process | None = None
        self.lines: list[Line] = []
        self.stray: list[str] = []
        self.stderr: list[str] = []
        self._stderr_task: asyncio.Task[None] | None = None

    def mcp_config(self) -> str:
        return json.dumps(
            {
                "mcpServers": {
                    "stand": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(MCP_SERVER), "stdio"],
                    }
                }
            }
        )

    async def start(self, *extra: str, env: Mapping[str, str] | None = None) -> None:
        environment = {
            **os.environ,
            "BOBA_LLM_KIND": "openai",
            "BOBA_LLM_BASE_URL": self._model.base_url(),
            "BOBA_LLM_MODEL": "fake-model",
            "BOBA_HISTORY_ROOT": str(self._root / "history"),
            "HOME": str(self._root / "home"),
        }
        if env is not None:
            environment.update(env)

        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            "--setting-sources",
            "user",
            *extra,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            cwd=str(self._root),
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    @property
    def process(self) -> asyncio.subprocess.Process:
        assert self._process is not None
        return self._process

    async def send(self, line: Mapping[str, Any]) -> None:
        await self.send_raw(json.dumps(line, ensure_ascii=False) + "\n")

    async def send_raw(self, text: str) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        stdin.write(text.encode("utf-8"))
        await stdin.drain()

    async def read(self) -> Line:
        stdout = self.process.stdout
        assert stdout is not None
        while True:
            try:
                raw = await asyncio.wait_for(stdout.readline(), READ_TIMEOUT_SEC)
            except TimeoutError as exc:
                tail = self.stderr[-15:]
                msg = f"no line from boba in {READ_TIMEOUT_SEC} s; stderr: {tail}"
                raise AssertionError(msg) from exc

            if not raw:
                msg = f"boba closed stdout; stderr tail: {self.stderr[-10:]}"
                raise AssertionError(msg)

            text = raw.decode("utf-8").rstrip("\n")
            try:
                line = json.loads(text)
            except json.JSONDecodeError:
                self.stray.append(text)
                continue

            assert isinstance(line, dict)
            self.lines.append(line)

            return line

    async def read_until(self, accept: Callable[[Line], bool]) -> Line:
        while True:
            line = await self.read()
            if accept(line):
                return line

    async def close_stdin(self) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        stdin.close()
        await stdin.wait_closed()

    async def wait(self) -> int:
        code = await asyncio.wait_for(self.process.wait(), READ_TIMEOUT_SEC)
        if self._stderr_task is not None:
            await self._stderr_task

        return code

    async def kill(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.kill()
            await self._process.wait()

    async def _drain_stderr(self) -> None:
        stderr = self.process.stderr
        assert stderr is not None
        while True:
            raw = await stderr.readline()
            if not raw:
                return

            self.stderr.append(raw.decode("utf-8", errors="replace").rstrip("\n"))


def journal_lines(root: Path, session: str) -> list[Line]:
    path = root / "history" / f"{session}.jsonl"
    found: list[Line] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        found.append(json.loads(raw))

    return found


@pytest.fixture
async def model() -> AsyncIterator[FakeModel]:
    fake = FakeModel()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


@pytest.fixture
async def boba(model: FakeModel, tmp_path: Path) -> AsyncIterator[BobaProcess]:
    process = BobaProcess(tmp_path, model)
    try:
        yield process
    finally:
        await process.kill()


class TestConversation:
    async def test_tool_permission_mode_cancel_interrupt_and_resume(
        self, boba: BobaProcess, model: FakeModel, tmp_path: Path
    ) -> None:
        await boba.start(
            "--session", "cli-1", "--mcp-config", boba.mcp_config(), "--record-requests"
        )
        init = await boba.read()
        assert init["type"] == "system"
        assert init["subtype"] == "init"
        assert init["session_id"] == "cli-1"
        assert init["resumed"] is False
        assert "add" in init["tools"]
        assert init["settings"]["model"] == "default"

        await self.tool_runs_without_a_question(boba)
        await self.permission_is_asked_and_granted(boba)
        await self.plan_mode_denies_the_tool(boba)
        await self.late_message_is_cancelled(boba)
        await self.interrupt_ends_the_turn(boba)

        await boba.close_stdin()
        assert await boba.wait() == 0
        assert boba.stray == [], boba.stray
        assert all("type" in line for line in boba.lines)

        records = journal_lines(tmp_path, "cli-1")
        types = [r["type"] for r in records]
        assert "api-request" in types
        assert "permission-rule" in types
        assert types.count("permission-mode") == 3
        pops = [r for r in records if r.get("operation") == "popOne"]
        assert [r["commandUuid"] for r in pops] == ["late-1"]
        texts = [
            r["message"]["content"]
            for r in records
            if r["type"] == "user" and isinstance(r["message"]["content"], str)
        ]
        assert "a late message" not in texts

        await self.resume_continues_the_session(boba, model, tmp_path)

    @staticmethod
    async def tool_runs_without_a_question(boba: BobaProcess) -> None:
        await boba.send(user(call({"name": "add", "arguments": {"a": 1, "b": 2}})))
        result = await boba.read_until(is_result)
        assert result["subtype"] == "completed"
        assert not any(is_question(line) for line in boba.lines)
        tool_results = [
            line
            for line in boba.lines
            if line["type"] == "user" and isinstance(line["message"]["content"], list)
        ]
        assert tool_results[-1]["message"]["content"][0]["content"] == "3"

    @staticmethod
    async def permission_is_asked_and_granted(boba: BobaProcess) -> None:
        await boba.send(user(call({"name": "shout", "arguments": {"text": "hi"}})))
        question = await boba.read_until(is_question)
        assert question["request"]["subtype"] == "can_use_tool"
        assert question["request"]["tool_name"] == "shout"
        await boba.send(allow(question["request_id"], rules=["shout"]))
        result = await boba.read_until(is_result)
        assert result["subtype"] == "completed"

    @staticmethod
    async def plan_mode_denies_the_tool(boba: BobaProcess) -> None:
        await boba.send(command("c-plan", "set_permission_mode", mode="plan"))
        response = await boba.read_until(is_response("c-plan"))
        assert response["response"]["subtype"] == "success"
        assert response["response"]["response"] == {"mode": "plan"}

        await boba.send(user(call({"name": "shout", "arguments": {"text": "again"}})))
        result = await boba.read_until(is_result)
        assert result["subtype"] == "completed"
        tool_results = [
            line
            for line in boba.lines
            if line["type"] == "user" and isinstance(line["message"]["content"], list)
        ]
        assert "plan mode" in tool_results[-1]["message"]["content"][0]["content"]

        await boba.send(command("c-default", "set_permission_mode", mode="default"))
        await boba.read_until(is_response("c-default"))

    @staticmethod
    async def late_message_is_cancelled(boba: BobaProcess) -> None:
        await boba.send(
            user(call({"name": "sleep_for", "arguments": {"seconds": 1.0}}))
        )
        question = await boba.read_until(is_question)
        await boba.send(user("a late message", uuid="late-1", priority="later"))
        await boba.send(
            command("c-cancel", "cancel_async_message", message_id="late-1")
        )
        response = await boba.read_until(is_response("c-cancel"))
        assert response["response"]["response"] == {"removed": True}
        await boba.send(allow(question["request_id"]))
        result = await boba.read_until(is_result)
        assert result["subtype"] == "completed"

    @staticmethod
    async def interrupt_ends_the_turn(boba: BobaProcess) -> None:
        await boba.send(user(call({"name": "sleep_for", "arguments": {"seconds": 30}})))
        question = await boba.read_until(is_question)
        await boba.send(allow(question["request_id"]))
        await asyncio.sleep(0.3)
        await boba.send(command("c-stop", "interrupt"))
        response = await boba.read_until(is_response("c-stop"))
        assert response["response"]["response"] == {"stopped": True}
        result = await boba.read_until(is_result)
        assert result["subtype"] == "aborted_tools"

    @staticmethod
    async def resume_continues_the_session(
        boba: BobaProcess, model: FakeModel, tmp_path: Path
    ) -> None:
        resumed = BobaProcess(tmp_path, model)
        try:
            await resumed.start(
                "--session", "cli-1", "--resume", "--mcp-config", boba.mcp_config()
            )
            init = await resumed.read()
            assert init["subtype"] == "init"
            assert init["resumed"] is True
            assert init["permission_mode"] == "default"
            await resumed.send(user(ScenarioName.ANSWER.value))
            result = await resumed.read_until(is_result)
            assert result["subtype"] == "completed"
            await resumed.close_stdin()
            assert await resumed.wait() == 0
        finally:
            await resumed.kill()


class TestRobustness:
    async def test_bad_lines_give_errors_and_the_process_lives(
        self, boba: BobaProcess
    ) -> None:
        await boba.start("--session", "cli-2")
        init = await boba.read()
        assert init["subtype"] == "init"

        await boba.send_raw("this is not json\n")
        error = await boba.read_until(is_error)
        assert "not valid JSON" in error["message"]

        await boba.send({"type": "no_such_type"})
        error = await boba.read_until(is_error)
        assert "does not match the protocol" in error["message"]

        await boba.send(command("c-x", "set_permission_mode", mode="auto"))
        response = await boba.read_until(is_response("c-x"))
        assert response["response"]["subtype"] == "error"
        assert "classifier" in response["response"]["error"]

        await boba.send(user(ScenarioName.ANSWER.value))
        result = await boba.read_until(is_result)
        assert result["subtype"] == "completed"

        await boba.send(command("c-end", "end_session"))
        await boba.read_until(is_response("c-end"))
        assert await boba.wait() == 0

    async def test_sigterm_exits_zero_with_a_flushed_journal(
        self, boba: BobaProcess, tmp_path: Path
    ) -> None:
        await boba.start("--session", "cli-3")
        await boba.send(user(ScenarioName.ANSWER.value))
        await boba.read_until(is_result)

        boba.process.send_signal(signal.SIGTERM)
        assert await boba.wait() == 0
        records = journal_lines(tmp_path, "cli-3")
        assert [r["type"] for r in records][-1] == "assistant"

    async def test_resume_of_an_unknown_session_fails(self, boba: BobaProcess) -> None:
        """Отказ приходит на старте, до чтения ввода."""
        await boba.start("--session", "nobody", "--resume")

        assert await boba.wait() == 1
        assert any("no journal to resume" in line for line in boba.stderr)

    async def test_initialize_appends_the_system_prompt(
        self, boba: BobaProcess, model: FakeModel
    ) -> None:
        """`initialize` после старта несёт добавку к системному промпту и
        отвечает возможностями процесса; лишние поля пропускаются."""
        await boba.start("--session", "cli-4", "--system-prompt", "Be terse.")
        init = await boba.read()
        assert init["subtype"] == "init"

        await boba.send(
            command(
                "c-init",
                "initialize",
                appendSystemPrompt="Answer in French.",
                hooks={},
                sdkMcpServers=[],
            )
        )
        response = await boba.read_until(is_response("c-init"))
        assert response["response"]["subtype"] == "success"
        payload = response["response"]["response"]
        assert payload["models"] == ["default"]
        assert payload["pid"] == boba.process.pid

        await boba.send(user(ScenarioName.ANSWER.value))
        await boba.read_until(is_result)
        system = model.app.requests[0]["messages"][0]
        assert system["role"] == "system"
        assert system["content"] == "Be terse.\n\nAnswer in French."

        await boba.send(command("c-again", "initialize", systemPrompt="Late."))
        response = await boba.read_until(is_response("c-again"))
        assert response["response"]["subtype"] == "success"

        await boba.close_stdin()
        assert await boba.wait() == 0
