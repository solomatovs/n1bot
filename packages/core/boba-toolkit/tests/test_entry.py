"""Контракт вызова: адрес, argv, каналы флагами, конверт, CLI настоящим
subprocess'ом."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from boba.stand_core.fake_toolmod import FakeConfig, fake_echo
from boba.toolkit.entry import (
    ArgumentTooLargeError,
    EntryFlag,
    ToolAddress,
    ToolArgv,
    ToolMain,
)
from boba.toolkit.protocol import REPLY, ReplyError, ReplyOk
from boba.toolkit.result import ExceptionResult

TESTS_DIR = str(Path(__file__).resolve().parent)

CFG = FakeConfig(token=SecretStr("s3cret-token"), limit=5)

TOOLSET = ToolMain.toolset(fake_echo)
FAKE = TOOLSET[0]


def run_module(
    arguments: list[str],
    config: bytes | None = None,
    result_fd: bool = False,
) -> tuple[subprocess.CompletedProcess[bytes], bytes]:
    """Запуск fake_toolmod настоящей командой модуля, как зовёт launcher.

    config едет каналом --injected-fd, конверт возвращается каналом
    --fd-result — номера дескрипторов в аргументах, как их шлёт лончер.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = TESTS_DIR + os.pathsep + env.get("PYTHONPATH", "")

    pass_fds: list[int] = []
    extra: list[str] = []

    config_r = -1
    if config is not None:
        config_r, config_w = os.pipe()
        os.write(config_w, config)
        os.close(config_w)
        extra.extend((EntryFlag.INJECTED_FD.value, str(config_r)))
        pass_fds.append(config_r)

    read_fd = -1
    write_fd = -1
    if result_fd:
        read_fd, write_fd = os.pipe()
        extra.extend((EntryFlag.FD_RESULT.value, str(write_fd)))
        pass_fds.append(write_fd)

    proc = subprocess.run(
        [sys.executable, "-m", "boba.stand_core.fake_toolmod", *arguments, *extra],
        capture_output=True,
        env=env,
        pass_fds=pass_fds,
        timeout=60,
        check=False,
    )

    if config_r >= 0:
        os.close(config_r)

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

        if address.module != "boba.stand_core.fake_toolmod":
            raise AssertionError('address.module == "boba.stand_core.fake_toolmod"')
        if address.name != "fake_echo":
            raise AssertionError('address.name == "fake_echo"')
        if address.argv_head()[1:] != [
            "-m",
            "boba.stand_core.fake_toolmod",
            "fake_echo",
        ]:
            raise AssertionError(
                'address.argv_head()[1:] == ["-m", "boba.stand_core.fake_toolmod", "…'
            )


class TestArgv:
    def test_llm_args_go_to_flags_injected_to_config(self) -> None:
        command = ToolArgv.render(
            ToolAddress.of(FAKE),
            ToolArgv.schema_of(FAKE),
            {"text": "hi there", "repeat": 3, "cfg": CFG},
            input_counts={},
        )

        argv = list(command.argv)
        if argv[4:] != ["--text", "hi there", "--repeat", "3"]:
            raise AssertionError('argv[4:] == ["--text", "hi there", "--repeat", "3"]')

        payload = json.loads(command.config)
        if payload["cfg"]["token"] != "s3cret-token":
            raise AssertionError('payload["cfg"]["token"] == "s3cret-token"')

    def test_secret_never_in_argv(self) -> None:
        command = ToolArgv.render(
            ToolAddress.of(FAKE),
            ToolArgv.schema_of(FAKE),
            {"text": "x", "repeat": 1, "cfg": CFG},
            input_counts={},
        )

        if "s3cret-token" in " ".join(command.argv):
            raise AssertionError('"s3cret-token" not in " ".join(command.argv)')

    def test_parse_restores_kwargs(self) -> None:
        command = ToolArgv.render(
            ToolAddress.of(FAKE),
            ToolArgv.schema_of(FAKE),
            {"text": "план б", "repeat": 2, "cfg": CFG},
            input_counts={},
        )

        kwargs = ToolArgv.parse(FAKE, command.argv[4:], command.config)

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
                input_counts={},
            )


class TestToolMainAsProgram:
    """Модуль инструментов — обычная программа: контракт argv/stdin/конверт."""

    CONFIG = json.dumps({"cfg": CFG.revealed()}).encode()

    def test_help_lists_tools(self) -> None:
        proc, _ = run_module(["--help"])

        if proc.returncode != 0:
            raise AssertionError("proc.returncode == 0")
        if b"fake_echo" not in proc.stdout:
            raise AssertionError('b"fake_echo" in proc.stdout')

    def test_human_run_prints_content(self) -> None:
        proc, _ = run_module(
            ["fake_echo", "--text", "ping", "--repeat", "2"],
            config=self.CONFIG,
        )

        if proc.returncode != 0:
            raise AssertionError("proc.returncode == 0")
        if "ping ping|s3cret-token" not in proc.stdout.decode():
            raise AssertionError('"ping ping|s3cret-token" in proc.stdout.decode()')

    def test_envelope_goes_to_result_fd_not_stdout(self) -> None:
        proc, envelope = run_module(
            ["fake_echo", "--text", "ping", "--repeat", "1"],
            config=self.CONFIG,
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
        if reply.artifact.kind != "markdown":
            raise AssertionError('reply.artifact.kind == "markdown"')

    def test_body_logs_land_on_stdout(self) -> None:
        """Логи тела — живой вывод: журнал и панель читают stdout процесса."""
        proc, envelope = run_module(
            ["fake_echo", "--text", "ping", "--repeat", "1"],
            config=self.CONFIG,
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

    def test_body_error_becomes_error_envelope(self) -> None:
        proc, envelope = run_module(
            ["fake_echo", "--text", "boom", "--repeat", "1"],
            config=self.CONFIG,
            result_fd=True,
        )

        if proc.returncode != ToolMain.Exit.FAILURE:
            raise AssertionError("proc.returncode == ToolMain.Exit.FAILURE")

        reply = REPLY.validate_json(envelope)
        if not (isinstance(reply, ReplyError)):
            raise AssertionError("isinstance(reply, ReplyError)")

        failure = reply.failure
        if not isinstance(failure, ExceptionResult):
            raise AssertionError(f"failure is ExceptionResult: {failure!r}")
        if failure.error_kind != "FakeUnavailableError":
            raise AssertionError(f"error_kind: {failure.error_kind!r}")
        if "fake backend is down" not in failure.llm_view():
            raise AssertionError(f"llm_view: {failure.llm_view()!r}")
        if "fake_toolmod.py" not in failure.raised_at:
            raise AssertionError(f"raised_at: {failure.raised_at!r}")

    def test_any_error_becomes_envelope_with_trace_in_log(self) -> None:
        """Ошибок «вне контракта» нет: любое исключение тела едет конвертом,
        трасса — в журнал процесса (stdout) и в сам результат."""
        proc, envelope = run_module(
            ["fake_echo", "--text", "crash", "--repeat", "1"],
            config=self.CONFIG,
            result_fd=True,
        )

        if proc.returncode != ToolMain.Exit.FAILURE:
            raise AssertionError(f"rc={proc.returncode} stderr={proc.stderr!r}")

        reply = REPLY.validate_json(envelope)
        if not isinstance(reply, ReplyError):
            raise AssertionError("isinstance(reply, ReplyError)")
        if reply.failure.error_kind != "RuntimeError":
            raise AssertionError(f"error_kind: {reply.failure.error_kind!r}")
        if "Traceback (most recent call last)" not in reply.failure.trace():
            raise AssertionError(f"trace: {reply.failure.trace()!r}")
        if b"Traceback (most recent call last)" not in proc.stdout:
            raise AssertionError(f"stdout={proc.stdout!r}")

    def test_unknown_tool_is_entry_error(self) -> None:
        proc, envelope = run_module(["no_such_tool"], result_fd=True)

        if proc.returncode != ToolMain.Exit.ENTRY_ERROR:
            raise AssertionError("proc.returncode == ToolMain.Exit.ENTRY_ERROR")

        reply = REPLY.validate_json(envelope)
        if not (isinstance(reply, ReplyError)):
            raise AssertionError("isinstance(reply, ReplyError)")
        if reply.failure.error_kind != "ToolEntryError":
            raise AssertionError(f"error_kind: {reply.failure.error_kind!r}")
        if "unknown tool 'no_such_tool'" not in reply.failure.llm_view():
            raise AssertionError(f"llm_view: {reply.failure.llm_view()!r}")

    def test_invalid_flag_is_entry_error(self) -> None:
        proc, envelope = run_module(
            ["fake_echo", "--nope", "x"],
            config=self.CONFIG,
            result_fd=True,
        )

        if proc.returncode != ToolMain.Exit.ENTRY_ERROR:
            raise AssertionError("proc.returncode == ToolMain.Exit.ENTRY_ERROR")

        reply = REPLY.validate_json(envelope)
        if not (isinstance(reply, ReplyError)):
            raise AssertionError("isinstance(reply, ReplyError)")
        if reply.failure.error_kind != "ToolEntryError":
            raise AssertionError(f"error_kind: {reply.failure.error_kind!r}")

    def test_missing_config_is_entry_error(self) -> None:
        proc, _ = run_module(["fake_echo", "--text", "x", "--repeat", "1"])

        if proc.returncode != ToolMain.Exit.ENTRY_ERROR:
            raise AssertionError("proc.returncode == ToolMain.Exit.ENTRY_ERROR")
        if b"ToolEntryError: " not in proc.stderr:
            raise AssertionError(f"stderr={proc.stderr!r}")

    def test_broken_channel_number_is_entry_error(self) -> None:
        """Не-числовой номер канала: конверт писать некуда, причина в stderr."""
        proc, _ = run_module(
            ["fake_echo", "--text", "x", "--repeat", "1", "--fd-result", "nope"]
        )

        if proc.returncode != ToolMain.Exit.ENTRY_ERROR:
            raise AssertionError("proc.returncode == ToolMain.Exit.ENTRY_ERROR")
        if b"expects a descriptor number" not in proc.stderr:
            raise AssertionError(f"stderr={proc.stderr!r}")

    def test_injected_file_serves_the_config(self, tmp_path: Path) -> None:
        """Ручной запуск: конфиг файлом --injected, каналов лончера нет."""
        injected = tmp_path / "injected.json"
        injected.write_bytes(self.CONFIG)

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
        if b"ToolEntryError: " not in proc.stderr:
            raise AssertionError(f"stderr={proc.stderr!r}")
