"""Канальный контракт ch-payload'а: запрос из tool_args, отказ конвертом.

Payload запускается настоящим процессом на реальных pipe-каналах; сервер при
этом недоступен намеренно — проверяется контракт каналов, а не SQL.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from boba.toolkit.channels import Channel, StreamFormat
from boba.toolkit.payload import PayloadExit

PASSWORD = "s3cret"

DEAD_CONNECTION: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 1,
    "interface": "http",
    "username": "u",
    "password": PASSWORD,
    "connect_timeout": 1,
}

ENTRY: tuple[str, ...] = ("-m", "boba.tool.ch.payload")


class PayloadRun(BaseModel):
    """Итог прогона payload'а: код возврата, конверт, данные и stderr."""

    model_config = ConfigDict(frozen=True)

    code: int
    envelope: dict[str, Any]
    payload: bytes
    stderr: str

    def error(self) -> dict[str, Any]:
        body = self.envelope["error"]
        assert isinstance(body, dict)

        return body


def _read_all(fd: int) -> bytes:
    data = bytearray()
    while True:
        piece = os.read(fd, 65536)
        if not piece:
            break
        data.extend(piece)

    os.close(fd)

    return bytes(data)


def _run(request: Mapping[str, Any], stdin: bytes) -> PayloadRun:
    args_r, args_w = os.pipe()
    result_r, result_w = os.pipe()
    payload_r, payload_w = os.pipe()
    stdin_r, stdin_w = os.pipe()

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(sys.path)
    env[Channel.TOOL_ARGS.env_name] = str(args_r)
    env[Channel.TOOL_RESULT.env_name] = str(result_w)
    env[Channel.TOOL_PAYLOAD.env_name] = str(payload_w)
    env[Channel.TOOL_STDIN.env_name] = str(stdin_r)
    env[Channel.TOOL_STDOUT.env_name] = "1"
    env[Channel.TOOL_STDERR.env_name] = "2"

    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, *ENTRY],
        pass_fds=(args_r, result_w, payload_w, stdin_r),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    os.close(args_r)
    os.close(result_w)
    os.close(payload_w)
    os.close(stdin_r)

    os.write(args_w, json.dumps(request, ensure_ascii=False).encode("utf-8"))
    os.close(args_w)

    os.write(stdin_w, stdin)
    os.close(stdin_w)

    _, stderr = proc.communicate(timeout=60)

    raw = _read_all(result_r).decode("utf-8").strip()
    payload = _read_all(payload_r)

    return PayloadRun(
        code=proc.returncode,
        envelope=json.loads(raw),
        payload=payload,
        stderr=stderr.decode("utf-8", errors="replace"),
    )


class TestChPayloadChannels:
    def test_unreachable_server_is_a_declared_failure(self) -> None:
        run = _run(
            {
                "op": "ch_query",
                "connection": DEAD_CONNECTION,
                "sql": "select 1",
                "params": {},
                "row_limit": 10,
            },
            b"",
        )

        assert run.code == PayloadExit.FAILURE
        assert run.error()["kind"] == "database_unavailable"
        assert run.payload == b""

    def test_insert_of_unreachable_server_reads_its_input(self) -> None:
        run = _run(
            {
                "op": "ch_insert",
                "connection": DEAD_CONNECTION,
                "table": "events",
                "stdin_format": StreamFormat.NDJSON,
            },
            b'{"a": 1}\n',
        )

        assert run.code == PayloadExit.FAILURE
        assert run.error()["kind"] == "database_unavailable"

    def test_uninsertable_input_format_is_rejected_by_the_model(self) -> None:
        run = _run(
            {
                "op": "ch_insert",
                "connection": DEAD_CONNECTION,
                "table": "events",
                "stdin_format": StreamFormat.TEXT,
            },
            b"plain\n",
        )

        assert run.code == PayloadExit.FAILURE
        assert run.error()["kind"] == "invalid_request"
        assert "stdin_format" in run.error()["message"]
        assert PASSWORD not in json.dumps(run.envelope, ensure_ascii=False)
