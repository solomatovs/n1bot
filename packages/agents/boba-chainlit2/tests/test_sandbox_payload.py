"""Payload-контракт: маркерная строка с JSON и жёсткий разбор результата."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from boba.chainlit2.process.runner import RunResult
from boba.chainlit2.sandbox import (
    SandboxCaller,
    SandboxOutcome,
    SandboxPayload,
    SandboxPayloadError,
    SandboxProfile,
)

_PROFILE_BASE: dict[str, Any] = {
    "rootfs": "",
    "ro_binds": ("/usr", "/bin", "/sbin", "/lib", "/lib64"),
    "rw_binds": (),
    "rw_images": (),
    "image_template": "",
    "launcher": {
        "mount_wait_sec": 10.0,
        "mount_poll_sec": 0.05,
        "shutdown_wait_sec": 5.0,
        "copy_chunk_bytes": 1 << 20,
    },
    "tmpfs": ("/tmp:64M",),  # noqa: S108
    "network": False,
    "env_set": {"PATH": "/usr/bin:/bin", "HOME": "/tmp"},  # noqa: S108
    "timeout_sec": 30,
    "max_memory_bytes": 512 * 1024 * 1024,
    "max_cpu_sec": 30,
    "max_file_size_bytes": 64 * 1024 * 1024,
    "max_open_files": 1024,
    "max_processes": 256,
    "max_output_bytes": 256 * 1024,
    "cgroup_base": "",
    "oom_score_adj": 0,
    "cwd": "/tmp",  # noqa: S108
}


class Answer(BaseModel):
    """Схема ответа payload'а в тестах."""

    text: str
    pages: int


class Request(BaseModel):
    """Схема запроса payload'а в тестах."""

    path: str


@pytest.fixture(autouse=True)
def chainlit_context() -> None:
    pass


def _profile(**kw: Any) -> SandboxProfile:
    return SandboxProfile.model_validate({**_PROFILE_BASE, **kw})


def _outcome(stdout: str, **kw: Any) -> SandboxOutcome:
    fields: dict[str, Any] = {
        "exit_code": 0,
        "stdout": stdout,
        "stderr": "",
        "truncated_stdout": False,
        "truncated_stderr": False,
        "duration_ms": 1,
        "timed_out": False,
    }
    fields.update(kw)
    return SandboxOutcome("doc", RunResult(**fields), kw.pop("diagnostic", ""))


class TestDecode:
    """Всё, что отклоняется от контракта, — ошибка, а не частичный результат."""

    def test_marker_line_is_parsed(self) -> None:
        line = SandboxPayload.encode(Answer(text="привет", pages=2))
        answer = SandboxPayload.decode(_outcome(line + "\n"), Answer)
        assert (answer.text, answer.pages) == ("привет", 2)

    def test_free_output_around_marker_is_ignored(self) -> None:
        line = SandboxPayload.encode(Answer(text="ok", pages=1))
        stdout = f"загружаю\n{line}\nготово\n"
        assert SandboxPayload.decode(_outcome(stdout), Answer).pages == 1

    def test_missing_marker_is_error(self) -> None:
        with pytest.raises(SandboxPayloadError, match="no 'sandbox-result:' line"):
            SandboxPayload.decode(_outcome("просто текст\n"), Answer)

    def test_two_markers_are_error(self) -> None:
        line = SandboxPayload.encode(Answer(text="ok", pages=1))
        with pytest.raises(SandboxPayloadError, match=r"2 .* lines"):
            SandboxPayload.decode(_outcome(f"{line}\n{line}\n"), Answer)

    def test_truncated_output_is_error(self) -> None:
        """Обрезанный JSON распарсить может и получиться — доверять нельзя."""
        line = SandboxPayload.encode(Answer(text="ok", pages=1))
        outcome = _outcome(line + "\n", truncated_stdout=True)
        with pytest.raises(SandboxPayloadError, match="output truncated"):
            SandboxPayload.decode(outcome, Answer)

    def test_timeout_is_error(self) -> None:
        with pytest.raises(SandboxPayloadError, match="timed out"):
            SandboxPayload.decode(_outcome("", timed_out=True), Answer)

    def test_nonzero_exit_is_error(self) -> None:
        line = SandboxPayload.encode(Answer(text="ok", pages=1))
        outcome = _outcome(line + "\n", exit_code=3, stderr="упал")
        with pytest.raises(SandboxPayloadError, match="exited with code 3"):
            SandboxPayload.decode(outcome, Answer)

    def test_stderr_is_shown_in_error(self) -> None:
        outcome = _outcome("", exit_code=1, stderr="Traceback: нет файла")
        with pytest.raises(SandboxPayloadError, match="нет файла"):
            SandboxPayload.decode(outcome, Answer)

    def test_broken_json_is_error(self) -> None:
        with pytest.raises(SandboxPayloadError, match="not valid JSON"):
            SandboxPayload.decode(_outcome("sandbox-result:{текст\n"), Answer)

    def test_schema_mismatch_is_error(self) -> None:
        stdout = 'sandbox-result:{"text": "ok"}\n'
        with pytest.raises(SandboxPayloadError, match="does not match Answer"):
            SandboxPayload.decode(_outcome(stdout), Answer)

    def test_plain_json_without_marker_is_error(self) -> None:
        """Маркер обязателен: иначе результат не отличить от вывода команды."""
        stdout = '{"text": "ok", "pages": 1}\n'
        with pytest.raises(SandboxPayloadError, match="no 'sandbox-result:' line"):
            SandboxPayload.decode(_outcome(stdout), Answer)


_PAYLOAD = """
import json, sys
from pathlib import Path

request = json.loads(sys.stdin.read())
print("payload: работаю", file=sys.stderr)
answer = {"text": Path(request["path"]).read_text(encoding="utf-8"), "pages": 1}
print("sandbox-result:" + json.dumps(answer, ensure_ascii=False))
"""


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap не установлен")
@pytest.mark.skipif(os.geteuid() == 0, reason="под root userns ведёт себя иначе")
class TestCallJson:
    """Payload реально исполняется внутри песочницы и возвращает структуру."""

    @staticmethod
    def _caller(tmp_path: Path, script: str, **profile_kw: Any) -> SandboxCaller:
        payload_dir = tmp_path / "payload"
        payload_dir.mkdir(parents=True, exist_ok=True)
        (payload_dir / "main.py").write_text(script, encoding="utf-8")
        profile = _profile(
            ro_binds=(*_PROFILE_BASE["ro_binds"], f"{payload_dir}:/opt/payload"),
            **profile_kw,
        )
        return SandboxCaller("doc", profile, dict)

    def test_request_and_answer_travel_through_stdin_stdout(
        self, tmp_path: Path
    ) -> None:
        doc = tmp_path / "payload" / "doc.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("содержимое", encoding="utf-8")
        caller = self._caller(tmp_path, _PAYLOAD)
        answer = caller.call_json(
            ["python3", "/opt/payload/main.py"],
            Request(path="/opt/payload/doc.txt"),
            Answer,
        )
        assert (answer.text, answer.pages) == ("содержимое", 1)

    def test_payload_is_read_only_inside(self, tmp_path: Path) -> None:
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "try:\n"
            "    Path('/opt/payload/main.py').write_text('взлом')\n"
            "except OSError as e:\n"
            "    print('sandbox-result:' + '{\"text\": \"%s\", \"pages\": 0}'"
            " % type(e).__name__)\n"
            "    sys.exit(0)\n"
            "print('sandbox-result:{\"text\": \"перезаписал\", \"pages\": 0}')\n"
        )
        caller = self._caller(tmp_path, script)
        answer = caller.call_json(
            ["python3", "/opt/payload/main.py"], Request(path=""), Answer
        )
        assert answer.text == "OSError"

    def test_payload_crash_is_reported(self, tmp_path: Path) -> None:
        script = "raise SystemExit('нет такого файла')\n"
        caller = self._caller(tmp_path, script)
        with pytest.raises(SandboxPayloadError, match="нет такого файла"):
            caller.call_json(
                ["python3", "/opt/payload/main.py"], Request(path=""), Answer
            )

    def test_payload_timeout_is_reported(self, tmp_path: Path) -> None:
        script = "import time\ntime.sleep(30)\n"
        caller = self._caller(tmp_path, script, timeout_sec=1)
        with pytest.raises(SandboxPayloadError, match="timed out"):
            caller.call_json(
                ["python3", "/opt/payload/main.py"], Request(path=""), Answer
            )

    def test_oversized_answer_is_reported(self, tmp_path: Path) -> None:
        script = (
            "import json\n"
            "print('sandbox-result:' + json.dumps("
            "{'text': 'x' * 200000, 'pages': 1}))\n"
        )
        caller = self._caller(tmp_path, script, max_output_bytes=4096)
        with pytest.raises(SandboxPayloadError, match="output truncated"):
            caller.call_json(
                ["python3", "/opt/payload/main.py"], Request(path=""), Answer
            )

    def test_entry_must_not_be_empty(self, tmp_path: Path) -> None:
        caller = self._caller(tmp_path, _PAYLOAD)
        with pytest.raises(ValueError, match="entry must not be empty"):
            caller.call_json([], Request(path=""), Answer)
