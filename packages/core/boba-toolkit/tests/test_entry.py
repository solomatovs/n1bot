"""Контракт вызова: аргументы одним JSON-объектом, каналы флагами, конверт,
подкоманда call программы плагина настоящим subprocess'ом."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from boba.stand_core.fake_toolmod import FakeConfig, fake_echo
from boba.toolkit.entry import (
    EntryFlag,
    ToolArgv,
    ToolMain,
)
from boba.toolkit.manifest import ManifestBuilder
from boba.toolkit.protocol import REPLY, ReplyError, ReplyOk
from boba.toolkit.result import ExceptionResult

TESTS_DIR = str(Path(__file__).resolve().parent)

CFG = FakeConfig(token=SecretStr("s3cret-token"), limit=5)

TOOLSET = ToolMain.toolset(fake_echo)
FAKE = TOOLSET[0]
DECL = ManifestBuilder().tool(FAKE)

PROGRAM = (
    sys.executable,
    "-m",
    "boba.sandbox.program",
    "--declared",
    "boba.stand_core.fake_toolmod:PLUGIN",
    "call",
)
"""Подкоманда call программы плагина над объявлением фейкового модуля."""


def run_module(
    arguments: list[str],
    args: dict[str, Any] | None = None,
    result_fd: bool = False,
) -> tuple[subprocess.CompletedProcess[bytes], bytes]:
    """Вызов фейкового инструмента подкомандой call, как зовёт launcher.

    args едут каналом --args-fd, конверт возвращается каналом --fd-result —
    номера дескрипторов в аргументах, как их шлёт лончер.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = TESTS_DIR + os.pathsep + env.get("PYTHONPATH", "")

    pass_fds: list[int] = []
    extra: list[str] = []

    args_r = -1
    if args is not None:
        args_r, args_w = os.pipe()
        os.write(args_w, json.dumps(args).encode())
        os.close(args_w)
        extra.extend((EntryFlag.ARGS_FD.value, str(args_r)))
        pass_fds.append(args_r)

    read_fd = -1
    write_fd = -1
    if result_fd:
        read_fd, write_fd = os.pipe()
        extra.extend((EntryFlag.FD_RESULT.value, str(write_fd)))
        pass_fds.append(write_fd)

    proc = subprocess.run(
        [*PROGRAM, *arguments, *extra],
        capture_output=True,
        env=env,
        pass_fds=pass_fds,
        timeout=60,
        check=False,
    )

    if args_r >= 0:
        os.close(args_r)

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


class TestArgv:
    def test_model_args_and_injected_go_to_one_object(self) -> None:
        command = ToolArgv.render(
            DECL,
            {"text": "hi there", "repeat": 3, "cfg": CFG},
            input_counts={},
            output_counts={},
        )

        if command.argv != ("fake_echo",):
            raise AssertionError(f"argv: {command.argv!r}")

        payload = json.loads(command.args)
        if payload["text"] != "hi there":
            raise AssertionError(f"payload: {payload!r}")
        if payload["cfg"]["token"] != "s3cret-token":
            raise AssertionError('payload["cfg"]["token"] == "s3cret-token"')

    def test_secret_never_in_argv(self) -> None:
        command = ToolArgv.render(DECL, {"text": "x", "repeat": 1, "cfg": CFG}, {}, {})

        if "s3cret-token" in " ".join(command.argv):
            raise AssertionError('"s3cret-token" not in " ".join(command.argv)')

    def test_parse_restores_kwargs(self) -> None:
        command = ToolArgv.render(
            DECL, {"text": "план б", "repeat": 2, "cfg": CFG}, {}, {}
        )

        kwargs = ToolArgv.parse(FAKE, (), command.args)

        if kwargs["text"] != "план б":
            raise AssertionError('kwargs["text"] == "план б"')
        if kwargs["repeat"] != 2:
            raise AssertionError('kwargs["repeat"] == 2')
        restored = kwargs["cfg"]
        if not (isinstance(restored, FakeConfig)):
            raise AssertionError("isinstance(restored, FakeConfig)")
        if restored.token.get_secret_value() != "s3cret-token":
            raise AssertionError('restored.token.get_secret_value() == "s3cret-token"')

    def test_large_argument_goes_through_the_channel(self) -> None:
        """Аргументы едут каналом, а не argv: потолка MAX_ARG_STRLEN нет."""
        text = "x" * 140_000
        command = ToolArgv.render(DECL, {"text": text, "repeat": 1, "cfg": CFG}, {}, {})

        kwargs = ToolArgv.parse(FAKE, (), command.args)

        if kwargs["text"] != text:
            raise AssertionError("the large argument arrived whole")

    def test_argument_is_checked_by_the_body(self) -> None:
        """Аргументы модели проверяет тело: ограничение поля — отказ разбора
        с именем поля, без значения в тексте."""
        command = ToolArgv.render(DECL, {"text": "", "repeat": 0, "cfg": CFG}, {}, {})

        try:
            ToolArgv.parse(FAKE, (), command.args)
        except Exception as exc:
            text = str(exc)
        else:
            raise AssertionError("empty text and zero repeat must be refused")

        if "text" not in text or "repeat" not in text:
            raise AssertionError(f"refusal names both fields: {text!r}")
        if "s3cret-token" in text:
            raise AssertionError(f"refusal does not quote values: {text!r}")


class TestToolMainAsProgram:
    """Подкоманда call — обычная программа: контракт argv/каналы/конверт."""

    CONFIG: dict[str, Any] = {"cfg": CFG.revealed()}

    def test_help_lists_tools(self) -> None:
        proc, _ = run_module(["--help"])

        if proc.returncode != 0:
            raise AssertionError("proc.returncode == 0")
        if b"fake_echo" not in proc.stdout:
            raise AssertionError('b"fake_echo" in proc.stdout')

    def test_human_run_prints_content(self) -> None:
        proc, _ = run_module(
            ["fake_echo"], args={**self.CONFIG, "text": "ping", "repeat": 2}
        )

        if proc.returncode != 0:
            raise AssertionError("proc.returncode == 0")
        if "ping ping|s3cret-token" not in proc.stdout.decode():
            raise AssertionError('"ping ping|s3cret-token" in proc.stdout.decode()')

    def test_envelope_goes_to_result_fd_not_stdout(self) -> None:
        proc, envelope = run_module(
            ["fake_echo"],
            args={**self.CONFIG, "text": "ping", "repeat": 1},
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
            ["fake_echo"],
            args={**self.CONFIG, "text": "ping", "repeat": 1},
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
            ["fake_echo"],
            args={**self.CONFIG, "text": "boom", "repeat": 1},
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
            ["fake_echo"],
            args={**self.CONFIG, "text": "crash", "repeat": 1},
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

    def test_unknown_argument_is_entry_error(self) -> None:
        proc, envelope = run_module(
            ["fake_echo"],
            args={**self.CONFIG, "text": "x", "repeat": 1, "nope": "x"},
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
        proc, _ = run_module(["fake_echo"], args={"text": "x", "repeat": 1})

        if proc.returncode != ToolMain.Exit.ENTRY_ERROR:
            raise AssertionError("proc.returncode == ToolMain.Exit.ENTRY_ERROR")
        if b"ToolEntryError: " not in proc.stderr:
            raise AssertionError(f"stderr={proc.stderr!r}")

    def test_broken_channel_number_is_entry_error(self) -> None:
        """Не-числовой номер канала: конверт писать некуда, причина в stderr."""
        proc, _ = run_module(["fake_echo", "--fd-result", "nope"])

        if proc.returncode != ToolMain.Exit.ENTRY_ERROR:
            raise AssertionError("proc.returncode == ToolMain.Exit.ENTRY_ERROR")
        if b"expects a descriptor number" not in proc.stderr:
            raise AssertionError(f"stderr={proc.stderr!r}")

    def test_args_file_serves_the_call(self, tmp_path: Path) -> None:
        """Ручной запуск: аргументы файлом --args, каналов лончера нет."""
        args = tmp_path / "args.json"
        args.write_text(json.dumps({**self.CONFIG, "text": "ping", "repeat": 1}))

        proc, _ = run_module(["fake_echo", "--args", str(args)])

        if proc.returncode != 0:
            raise AssertionError(f"proc.returncode == 0: {proc.stderr!r}")
        if b"ping" not in proc.stdout:
            raise AssertionError('b"ping" in proc.stdout')

    def test_unreadable_args_file_is_entry_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent.json"

        proc, _ = run_module(["fake_echo", "--args", str(missing)])

        if proc.returncode != ToolMain.Exit.ENTRY_ERROR:
            raise AssertionError("proc.returncode == ToolMain.Exit.ENTRY_ERROR")
        if b"ToolEntryError: " not in proc.stderr:
            raise AssertionError(f"stderr={proc.stderr!r}")
