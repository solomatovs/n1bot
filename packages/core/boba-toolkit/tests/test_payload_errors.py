"""Контракт канального payload'а: ожидаемое — конвертом, неожиданное — трейсбеком."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from boba.toolkit.channels import Channel, LogFrame
from boba.toolkit.payload import PayloadExit

TOOLKIT_SRC = Path(__file__).resolve().parents[1] / "src"

SCRIPT = """
import logging
import sys
from collections.abc import Mapping
from typing import ClassVar

from pydantic import BaseModel, Field

from boba.toolkit.payload import PayloadChannels, PayloadEntry, PayloadError


class Request(BaseModel):
    op: str = Field(min_length=1)
    mode: str = Field(min_length=1)
    size: int = Field(ge=1)


class Trailer(BaseModel):
    size: int


class UnreadableError(Exception):
    pass


class SilentError(Exception):
    pass


class Ops:
    LOG: ClassVar[logging.Logger] = logging.getLogger("boba.tool.probe")

    EXPECTED: ClassVar[Mapping[type[Exception], str]] = {
        UnreadableError: "document_unreadable",
        SilentError: "silent_failure",
    }

    REQUESTS: ClassVar[Mapping[str, type[BaseModel]]] = {"probe": Request}

    @classmethod
    async def dispatch(
        cls, request: BaseModel, channels: PayloadChannels
    ) -> BaseModel:
        assert isinstance(request, Request)
        if request.mode == "logs":
            cls.LOG.info("работаю: size=%d", request.size)
            cls.LOG.warning("что-то подозрительное")
            return Trailer(size=request.size)
        if request.mode == "ok":
            channels.payload().write(b"data-line\\n")
            return Trailer(size=request.size)
        if request.mode == "spew":
            stream = channels.payload()
            while True:
                stream.write(b"x" * 65536)
                stream.flush()
        if request.mode == "declared":
            raise UnreadableError("формат .md не читается")
        if request.mode == "silent":
            raise SilentError
        if request.mode == "explicit":
            raise PayloadError("bad_spec", "спека не по схеме")
        raise KeyError(request.mode)


sys.exit(PayloadEntry.main(Ops))
"""


class PayloadRun(BaseModel):
    """Итог прогона тестового payload'а: код, конверт, данные, stderr."""

    model_config = ConfigDict(frozen=True)

    code: int
    envelope: dict[str, Any] | None
    payload: bytes
    stderr: str

    def error(self) -> dict[str, Any]:
        assert self.envelope is not None
        body = self.envelope["error"]
        assert isinstance(body, dict)
        return body

    def log_frames(self) -> list[LogFrame]:
        frames: list[LogFrame] = []
        for line in self.stderr.splitlines():
            if LogFrame.matches(line):
                frames.append(LogFrame.decode(line))
        return frames


def _read_all(fd: int) -> bytes:
    data = bytearray()
    while True:
        piece = os.read(fd, 65536)
        if not piece:
            break
        data.extend(piece)
    os.close(fd)
    return bytes(data)


def _run(
    request: Mapping[str, Any] | str,
    *,
    env_channels: Mapping[Channel, str] | None = None,
    close_payload_reader: bool = False,
) -> PayloadRun:
    """Прогон payload'а отдельным процессом на реальных pipe-каналах."""
    args_r, args_w = os.pipe()
    result_r, result_w = os.pipe()
    payload_r, payload_w = os.pipe()

    env = dict(os.environ)
    env["PYTHONPATH"] = f"{TOOLKIT_SRC}{os.pathsep}{env.get('PYTHONPATH', '')}"

    channel_env: Mapping[Channel, str]
    if env_channels is not None:
        channel_env = env_channels
    else:
        channel_env = {
            Channel.TOOL_ARGS: str(args_r),
            Channel.TOOL_RESULT: str(result_w),
            Channel.TOOL_PAYLOAD: str(payload_w),
            Channel.TOOL_STDOUT: "1",
            Channel.TOOL_STDERR: "2",
        }
    for channel, value in channel_env.items():
        env[channel.env_name] = value

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", SCRIPT],
        pass_fds=(args_r, result_w, payload_w),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    os.close(args_r)
    os.close(result_w)
    os.close(payload_w)

    if close_payload_reader:
        os.close(payload_r)

    body: str
    if isinstance(request, str):
        body = request
    else:
        body = json.dumps(request, ensure_ascii=False)
    os.write(args_w, body.encode("utf-8"))
    os.close(args_w)

    _, stderr = proc.communicate(timeout=30)

    result_raw = _read_all(result_r).decode("utf-8").strip()

    payload = b""
    if not close_payload_reader:
        payload = _read_all(payload_r)

    envelope: dict[str, Any] | None = None
    if result_raw:
        envelope = json.loads(result_raw)

    return PayloadRun(
        code=proc.returncode,
        envelope=envelope,
        payload=payload,
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def _probe(mode: str, size: int = 1) -> dict[str, Any]:
    return {"op": "probe", "mode": mode, "size": size}


class TestExpectedErrors:
    """Объявленный отказ уезжает конвертом: пользователь видит причину, не стек."""

    def test_declared_type_becomes_error_envelope(self) -> None:
        run = _run(_probe("declared"))
        assert run.code == int(PayloadExit.FAILURE)
        error = run.error()
        assert error["kind"] == "document_unreadable"
        assert "формат .md не читается" in error["message"]

    def test_explicit_payload_error_keeps_its_wording(self) -> None:
        run = _run(_probe("explicit"))
        assert run.code == int(PayloadExit.FAILURE)
        assert (run.error()["kind"], run.error()["message"]) == (
            "bad_spec",
            "спека не по схеме",
        )

    def test_empty_reason_falls_back_to_type_name(self) -> None:
        """Конверт требует непустой текст: у молчаливой ошибки берём её имя."""
        run = _run(_probe("silent"))
        assert run.error()["message"] == "SilentError"

    def test_reason_goes_to_stderr_without_traceback(self) -> None:
        """Оператору — одна запись в журнал, а не простыня стека."""
        run = _run(_probe("declared"))
        frames = run.log_frames()
        assert len(frames) == 1
        assert frames[0].lvl == "ERROR"
        assert "Traceback" not in run.stderr


class TestInvalidRequest:
    """Битый запрос — ожидаемый отказ на границе разбора, без эха ввода."""

    def test_broken_json_is_invalid_request(self) -> None:
        run = _run("{broken")
        assert run.code == int(PayloadExit.FAILURE)
        assert run.error()["kind"] == "invalid_request"
        assert "Traceback" not in run.stderr

    def test_schema_violation_names_field_but_not_value(self) -> None:
        """Сводка валидации без значений полей: tool_args несёт секреты."""
        request = _probe("ok", size=0)
        request["password"] = "s3cret-echo"
        run = _run(request)
        error = run.error()
        assert error["kind"] == "invalid_request"
        assert "size" in error["message"]
        assert "s3cret-echo" not in error["message"]
        assert "s3cret-echo" not in run.stderr

    def test_missing_op_is_invalid_request(self) -> None:
        run = _run({"mode": "ok", "size": 1})
        assert run.error()["kind"] == "invalid_request"
        assert "op" in run.error()["message"]

    def test_unknown_op_names_known_ops(self) -> None:
        run = _run({"op": "нет-такой", "mode": "ok", "size": 1})
        error = run.error()
        assert error["kind"] == "invalid_request"
        assert "probe" in error["message"]


class TestUnexpectedErrors:
    """Необъявленное не прячется: интерпретатор печатает трейсбек сам."""

    def test_undeclared_type_propagates(self) -> None:
        run = _run(_probe("неизвестный"))
        assert run.code != 0
        assert run.envelope is None
        assert "KeyError" in run.stderr
        assert "Traceback" in run.stderr


class TestChannelContract:
    """Нарушение контракта запуска — падение в точке разбора env."""

    def test_missing_required_channel_fails_loudly(self) -> None:
        run = _run(
            _probe("ok"),
            env_channels={
                Channel.TOOL_RESULT: "3",
                Channel.TOOL_STDOUT: "1",
                Channel.TOOL_STDERR: "2",
            },
        )
        assert run.code != 0
        assert "required channel is not declared" in run.stderr


class TestLogging:
    """Логи инструмента уезжают кадрами в stderr — на исход они не влияют."""

    def test_log_frames_go_to_stderr(self) -> None:
        run = _run(_probe("logs", size=3))
        assert run.code == 0
        levels = [frame.lvl for frame in run.log_frames()]
        assert "INFO" in levels
        assert "WARNING" in levels
        assert run.log_frames()[0].name == "boba.tool.probe"
        assert "size=3" in run.log_frames()[0].msg

    def test_declared_error_is_logged_too(self) -> None:
        """Отказ виден и в журнале, но решает его конверт в tool_result."""
        run = _run(_probe("declared"))
        frames = run.log_frames()
        assert [frame.lvl for frame in frames] == ["ERROR"]
        assert "document_unreadable" in frames[0].msg
        assert run.error()["kind"] == "document_unreadable"


class TestSuccess:
    """Штатный путь: данные сырыми байтами, квитанция с честным bytes_out."""

    def test_payload_bytes_and_trailer_envelope(self) -> None:
        run = _run(_probe("ok", size=7))
        assert run.code == 0
        assert run.payload == b"data-line\n"
        assert run.envelope == {"bytes_out": 10, "data": {"size": 7}}

    def test_trailer_only_run_reports_zero_bytes(self) -> None:
        run = _run(_probe("logs", size=2))
        assert run.code == 0
        assert run.payload == b""
        assert run.envelope == {"bytes_out": 0, "data": {"size": 2}}


class TestConsumerGone:
    """Уход потребителя — шелльный SIGPIPE-исход: rc 141 и квитанция на месте."""

    def test_broken_payload_pipe_exits_141_with_receipt(self) -> None:
        run = _run(_probe("spew"), close_payload_reader=True)
        assert run.code == int(PayloadExit.CONSUMER_GONE)
        assert run.envelope is not None
        assert run.envelope["data"] == {}
        assert "Traceback" not in run.stderr
