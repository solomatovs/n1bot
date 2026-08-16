"""Контракт вызова: адрес, argv/stdin, конверт, CLI настоящим subprocess'ом."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fake_toolmod import EXPECTED, FakeConfig, FakeUnavailableError, fake_echo
from pydantic import SecretStr

from boba.toolkit.channels import ToolChannel
from boba.toolkit.entry import (
    REPLY,
    ArgumentTooLargeError,
    EntryErrorKind,
    ExpectedErrors,
    ReplyError,
    ReplyOk,
    ToolAddress,
    ToolArgv,
    ToolMain,
)

TESTS_DIR = str(Path(__file__).resolve().parent)

CFG = FakeConfig(token=SecretStr("s3cret-token"), limit=5)

TOOLSET = ToolMain.toolset(fake_echo)
FAKE = TOOLSET[0]


def run_module(
    arguments: list[str],
    stdin: bytes = b"",
    result_fd: bool = False,
) -> tuple[subprocess.CompletedProcess[bytes], bytes]:
    """Запуск fake_toolmod настоящей командой модуля, как зовёт launcher."""
    env = dict(os.environ)
    env["PYTHONPATH"] = TESTS_DIR + os.pathsep + env.get("PYTHONPATH", "")

    pass_fds: tuple[int, ...] = ()
    read_fd = -1
    if result_fd:
        read_fd, write_fd = os.pipe()
        os.set_inheritable(write_fd, True)
        env[ToolChannel.RESULT.env_name] = str(write_fd)
        pass_fds = (write_fd,)

    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "fake_toolmod", *arguments],
        input=stdin,
        capture_output=True,
        env=env,
        pass_fds=pass_fds,
        timeout=60,
        check=False,
    )

    envelope = b""
    if result_fd:
        os.close(write_fd)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(read_fd)
        envelope = b"".join(chunks)

    return proc, envelope


class TestAddress:
    def test_roundtrip(self) -> None:
        address = ToolAddress.of(FAKE)

        assert address.module == "fake_toolmod"
        assert address.name == "fake_echo"
        assert address.argv_head()[1:] == ["-m", "fake_toolmod", "fake_echo"]


class TestArgv:
    def test_llm_args_go_to_flags_injected_to_stdin(self) -> None:
        command = ToolArgv.render(
            ToolAddress.of(FAKE),
            ToolArgv.schema_of(FAKE),
            {"text": "hi there", "repeat": 3, "cfg": CFG},
        )

        argv = list(command.argv)
        assert argv[4:] == ["--text", "hi there", "--repeat", "3"]

        payload = json.loads(command.stdin)
        assert payload["cfg"]["token"] == "s3cret-token"

    def test_secret_never_in_argv(self) -> None:
        command = ToolArgv.render(
            ToolAddress.of(FAKE),
            ToolArgv.schema_of(FAKE),
            {"text": "x", "repeat": 1, "cfg": CFG},
        )

        assert "s3cret-token" not in " ".join(command.argv)

    def test_parse_restores_kwargs(self) -> None:
        command = ToolArgv.render(
            ToolAddress.of(FAKE),
            ToolArgv.schema_of(FAKE),
            {"text": "план б", "repeat": 2, "cfg": CFG},
        )

        kwargs = ToolArgv.parse(FAKE, command.argv[4:], command.stdin)

        assert kwargs["text"] == "план б"
        assert kwargs["repeat"] == 2
        restored = kwargs["cfg"]
        assert isinstance(restored, FakeConfig)
        assert restored.token.get_secret_value() == "s3cret-token"

    def test_oversized_argument_is_refused(self) -> None:
        with pytest.raises(ArgumentTooLargeError):
            ToolArgv.render(
                ToolAddress.of(FAKE),
                ToolArgv.schema_of(FAKE),
                {"text": "x" * 140_000, "repeat": 1, "cfg": CFG},
            )


class TestExpectedErrors:
    def test_subclass_matches_by_mro(self) -> None:
        class DerivedUnavailableError(FakeUnavailableError):
            pass

        kind = ExpectedErrors.kind_of(DerivedUnavailableError("x"), dict(EXPECTED))
        assert kind == "fake_unavailable"

    def test_unknown_error_gives_none(self) -> None:
        assert ExpectedErrors.kind_of(ValueError("x"), dict(EXPECTED)) is None


class TestToolMainAsProgram:
    """Модуль инструментов — обычная программа: контракт argv/stdin/конверт."""

    STDIN = json.dumps({"cfg": CFG.revealed()}).encode()

    def test_help_lists_tools(self) -> None:
        proc, _ = run_module(["--help"])

        assert proc.returncode == 0
        assert b"fake_echo" in proc.stdout

    def test_human_run_prints_content(self) -> None:
        proc, _ = run_module(
            ["fake_echo", "--text", "ping", "--repeat", "2"],
            stdin=self.STDIN,
        )

        assert proc.returncode == 0
        assert "ping ping|s3cret-token" in proc.stdout.decode()

    def test_envelope_goes_to_result_fd_not_stdout(self) -> None:
        proc, envelope = run_module(
            ["fake_echo", "--text", "ping", "--repeat", "1"],
            stdin=self.STDIN,
            result_fd=True,
        )

        assert proc.returncode == 0
        assert b'"status"' not in proc.stdout, "конверт не должен попасть в stdout"

        reply = REPLY.validate_json(envelope)
        assert isinstance(reply, ReplyOk)
        assert "ping|s3cret-token" in reply.content
        assert reply.artifact.kind == "text"

    def test_body_logs_land_on_stdout(self) -> None:
        """Логи тела — живой вывод: журнал и панель читают stdout процесса."""
        proc, envelope = run_module(
            ["fake_echo", "--text", "ping", "--repeat", "1"],
            stdin=self.STDIN,
            result_fd=True,
        )

        assert proc.returncode == 0

        stdout = proc.stdout.decode()
        assert "echo progress: ping" in stdout
        assert "INFO fake.tool" in stdout
        assert b"echo progress" not in envelope

    def test_expected_error_becomes_error_envelope(self) -> None:
        proc, envelope = run_module(
            ["fake_echo", "--text", "boom", "--repeat", "1"],
            stdin=self.STDIN,
            result_fd=True,
        )

        assert proc.returncode == ToolMain.Exit.EXPECTED_FAILURE

        reply = REPLY.validate_json(envelope)
        assert isinstance(reply, ReplyError)
        assert reply.kind == "fake_unavailable"
        assert "fake backend is down" in reply.message

    def test_unexpected_error_leaves_no_envelope(self) -> None:
        proc, envelope = run_module(
            ["fake_echo", "--text", "crash", "--repeat", "1"],
            stdin=self.STDIN,
            result_fd=True,
        )

        # правило разбора: ненулевой rc без конверта — неожиданное падение
        assert proc.returncode != 0
        assert envelope == b""
        assert b"RuntimeError" in proc.stderr

    def test_unknown_tool_is_entry_error(self) -> None:
        proc, envelope = run_module(["no_such_tool"], result_fd=True)

        assert proc.returncode == ToolMain.Exit.ENTRY_ERROR

        reply = REPLY.validate_json(envelope)
        assert isinstance(reply, ReplyError)
        assert reply.kind == str(EntryErrorKind.UNKNOWN_TOOL)

    def test_invalid_flag_is_entry_error(self) -> None:
        proc, envelope = run_module(
            ["fake_echo", "--nope", "x"],
            stdin=self.STDIN,
            result_fd=True,
        )

        assert proc.returncode == ToolMain.Exit.ENTRY_ERROR

        reply = REPLY.validate_json(envelope)
        assert isinstance(reply, ReplyError)
        assert reply.kind == str(EntryErrorKind.INVALID_REQUEST)

    def test_missing_config_is_entry_error(self) -> None:
        proc, _ = run_module(["fake_echo", "--text", "x", "--repeat", "1"])

        assert proc.returncode == ToolMain.Exit.ENTRY_ERROR
        assert b"invalid_request" in proc.stderr
