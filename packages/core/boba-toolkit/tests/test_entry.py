"""Контракт вызова: адрес, argv/stdin, конверт, CLI настоящим subprocess'ом."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from boba.stand.fake_toolmod import (
    EXPECTED,
    FakeConfig,
    FakeUnavailableError,
    fake_echo,
)
from boba.toolkit.channels import ToolChannel
from boba.toolkit.entry import (
    ArgumentTooLargeError,
    EntryErrorKind,
    ExpectedErrors,
    ToolAddress,
    ToolArgv,
    ToolMain,
)
from boba.toolkit.protocol import REPLY, ReplyError, ReplyOk

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
    write_fd = -1
    if result_fd:
        read_fd, write_fd = os.pipe()
        os.set_inheritable(write_fd, True)
        env[ToolChannel.RESULT.env_name] = str(write_fd)
        pass_fds = (write_fd,)

    proc = subprocess.run(
        [sys.executable, "-m", "boba.stand.fake_toolmod", *arguments],
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

        if address.module != "boba.stand.fake_toolmod":
            raise AssertionError('address.module == "boba.stand.fake_toolmod"')
        if address.name != "fake_echo":
            raise AssertionError('address.name == "fake_echo"')
        if address.argv_head()[1:] != ["-m", "boba.stand.fake_toolmod", "fake_echo"]:
            raise AssertionError(
                'address.argv_head()[1:] == ["-m", "boba.stand.fake_toolmod", "…'
            )


class TestArgv:
    def test_llm_args_go_to_flags_injected_to_stdin(self) -> None:
        command = ToolArgv.render(
            ToolAddress.of(FAKE),
            ToolArgv.schema_of(FAKE),
            {"text": "hi there", "repeat": 3, "cfg": CFG},
        )

        argv = list(command.argv)
        if argv[4:] != ["--text", "hi there", "--repeat", "3"]:
            raise AssertionError('argv[4:] == ["--text", "hi there", "--repeat", "3"]')

        payload = json.loads(command.stdin)
        if payload["cfg"]["token"] != "s3cret-token":
            raise AssertionError('payload["cfg"]["token"] == "s3cret-token"')

    def test_secret_never_in_argv(self) -> None:
        command = ToolArgv.render(
            ToolAddress.of(FAKE),
            ToolArgv.schema_of(FAKE),
            {"text": "x", "repeat": 1, "cfg": CFG},
        )

        if "s3cret-token" in " ".join(command.argv):
            raise AssertionError('"s3cret-token" not in " ".join(command.argv)')

    def test_parse_restores_kwargs(self) -> None:
        command = ToolArgv.render(
            ToolAddress.of(FAKE),
            ToolArgv.schema_of(FAKE),
            {"text": "план б", "repeat": 2, "cfg": CFG},
        )

        kwargs = ToolArgv.parse(FAKE, command.argv[4:], command.stdin)

        if kwargs["text"] != "план б":
            raise AssertionError('kwargs["text"] == "план б"')
        if kwargs["repeat"] != 2:
            raise AssertionError('kwargs["repeat"] == 2')
        restored = kwargs["cfg"]
        if not (isinstance(restored, FakeConfig)):
            raise AssertionError("isinstance(restored, FakeConfig)")
        if restored.token.get_secret_value() != "s3cret-token":
            raise AssertionError('restored.token.get_secret_value() == "s3cret-token"')

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
        if kind != "fake_unavailable":
            raise AssertionError('kind == "fake_unavailable"')

    def test_unknown_error_gives_none(self) -> None:
        if ExpectedErrors.kind_of(ValueError("x"), dict(EXPECTED)) is not None:
            raise AssertionError('ExpectedErrors.kind_of(ValueError("x"), dict(EXPECT…')


class TestToolMainAsProgram:
    """Модуль инструментов — обычная программа: контракт argv/stdin/конверт."""

    STDIN = json.dumps({"cfg": CFG.revealed()}).encode()

    def test_help_lists_tools(self) -> None:
        proc, _ = run_module(["--help"])

        if proc.returncode != 0:
            raise AssertionError("proc.returncode == 0")
        if b"fake_echo" not in proc.stdout:
            raise AssertionError('b"fake_echo" in proc.stdout')

    def test_human_run_prints_content(self) -> None:
        proc, _ = run_module(
            ["fake_echo", "--text", "ping", "--repeat", "2"],
            stdin=self.STDIN,
        )

        if proc.returncode != 0:
            raise AssertionError("proc.returncode == 0")
        if "ping ping|s3cret-token" not in proc.stdout.decode():
            raise AssertionError('"ping ping|s3cret-token" in proc.stdout.decode()')

    def test_envelope_goes_to_result_fd_not_stdout(self) -> None:
        proc, envelope = run_module(
            ["fake_echo", "--text", "ping", "--repeat", "1"],
            stdin=self.STDIN,
            result_fd=True,
        )

        if proc.returncode != 0:
            raise AssertionError("proc.returncode == 0")
        if b'"status"' in proc.stdout:
            raise AssertionError("конверт не должен попасть в stdout")

        reply = REPLY.validate_json(envelope)
        if not (isinstance(reply, ReplyOk)):
            raise AssertionError("isinstance(reply, ReplyOk)")
        if "ping|s3cret-token" not in reply.content:
            raise AssertionError('"ping|s3cret-token" in reply.content')
        if reply.artifact.kind != "text":
            raise AssertionError('reply.artifact.kind == "text"')

    def test_body_logs_land_on_stdout(self) -> None:
        """Логи тела — живой вывод: журнал и панель читают stdout процесса."""
        proc, envelope = run_module(
            ["fake_echo", "--text", "ping", "--repeat", "1"],
            stdin=self.STDIN,
            result_fd=True,
        )

        if proc.returncode != 0:
            raise AssertionError("proc.returncode == 0")

        stdout = proc.stdout.decode()
        if "echo progress: ping" not in stdout:
            raise AssertionError('"echo progress: ping" in stdout')
        if "INFO fake.tool" not in stdout:
            raise AssertionError('"INFO fake.tool" in stdout')
        if b"echo progress" in envelope:
            raise AssertionError('b"echo progress" not in envelope')

    def test_expected_error_becomes_error_envelope(self) -> None:
        proc, envelope = run_module(
            ["fake_echo", "--text", "boom", "--repeat", "1"],
            stdin=self.STDIN,
            result_fd=True,
        )

        if proc.returncode != ToolMain.Exit.EXPECTED_FAILURE:
            raise AssertionError("proc.returncode == ToolMain.Exit.EXPECTED_FAILURE")

        reply = REPLY.validate_json(envelope)
        if not (isinstance(reply, ReplyError)):
            raise AssertionError("isinstance(reply, ReplyError)")
        if reply.kind != "fake_unavailable":
            raise AssertionError('reply.kind == "fake_unavailable"')
        if "fake backend is down" not in reply.message:
            raise AssertionError('"fake backend is down" in reply.message')

    def test_unexpected_error_leaves_no_envelope(self) -> None:
        proc, envelope = run_module(
            ["fake_echo", "--text", "crash", "--repeat", "1"],
            stdin=self.STDIN,
            result_fd=True,
        )

        # правило разбора: ненулевой rc без конверта — неожиданное падение
        if proc.returncode == 0:
            raise AssertionError("proc.returncode != 0")
        if envelope != b"":
            raise AssertionError('envelope == b""')
        if b"RuntimeError" not in proc.stderr:
            raise AssertionError('b"RuntimeError" in proc.stderr')

    def test_unknown_tool_is_entry_error(self) -> None:
        proc, envelope = run_module(["no_such_tool"], result_fd=True)

        if proc.returncode != ToolMain.Exit.ENTRY_ERROR:
            raise AssertionError("proc.returncode == ToolMain.Exit.ENTRY_ERROR")

        reply = REPLY.validate_json(envelope)
        if not (isinstance(reply, ReplyError)):
            raise AssertionError("isinstance(reply, ReplyError)")
        if reply.kind != str(EntryErrorKind.UNKNOWN_TOOL):
            raise AssertionError("reply.kind == str(EntryErrorKind.UNKNOWN_TOOL)")

    def test_invalid_flag_is_entry_error(self) -> None:
        proc, envelope = run_module(
            ["fake_echo", "--nope", "x"],
            stdin=self.STDIN,
            result_fd=True,
        )

        if proc.returncode != ToolMain.Exit.ENTRY_ERROR:
            raise AssertionError("proc.returncode == ToolMain.Exit.ENTRY_ERROR")

        reply = REPLY.validate_json(envelope)
        if not (isinstance(reply, ReplyError)):
            raise AssertionError("isinstance(reply, ReplyError)")
        if reply.kind != str(EntryErrorKind.INVALID_REQUEST):
            raise AssertionError("reply.kind == str(EntryErrorKind.INVALID_REQUEST)")

    def test_missing_config_is_entry_error(self) -> None:
        proc, _ = run_module(["fake_echo", "--text", "x", "--repeat", "1"])

        if proc.returncode != ToolMain.Exit.ENTRY_ERROR:
            raise AssertionError("proc.returncode == ToolMain.Exit.ENTRY_ERROR")
        if b"invalid_request" not in proc.stderr:
            raise AssertionError('b"invalid_request" in proc.stderr')

    def test_injected_file_replaces_stdin(self, tmp_path: Path) -> None:
        injected = tmp_path / "injected.json"
        injected.write_bytes(self.STDIN)

        proc, _ = run_module(
            [
                "fake_echo",
                "--text",
                "ping",
                "--repeat",
                "1",
                "--injected",
                str(injected),
            ]
        )

        if proc.returncode != 0:
            raise AssertionError(f"proc.returncode == 0: {proc.stderr!r}")
        if b"ping" not in proc.stdout:
            raise AssertionError('b"ping" in proc.stdout')

    def test_unreadable_injected_file_is_entry_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent.json"

        proc, _ = run_module(
            ["fake_echo", "--text", "x", "--repeat", "1", "--injected", str(missing)]
        )

        if proc.returncode != ToolMain.Exit.ENTRY_ERROR:
            raise AssertionError("proc.returncode == ToolMain.Exit.ENTRY_ERROR")
        if b"invalid_request" not in proc.stderr:
            raise AssertionError('b"invalid_request" in proc.stderr')
