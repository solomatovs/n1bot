"""Payload-контракт: кадры данных плюс трейлер и жёсткий разбор результата."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from boba.sandbox import (
    SandboxCaller,
    SandboxOutcome,
    SandboxPayload,
    SandboxPayloadError,
    SandboxProfile,
)
from boba.sandbox.caller import SandboxFrameDecoder
from boba.sandbox.process_runner import RunResult
from boba.toolkit.launcher import (
    LauncherError,
    NoChunks,
    PayloadFailureError,
    TextCollector,
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
        "lock_wait_sec": 10.0,
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
    """Схема трейлера payload'а в тестах."""

    text: str
    pages: int


class Trailer(BaseModel):
    """Трейлер потокового payload'а: данные уехали кадрами."""

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


def _decode(stdout: str, schema: type[Answer], **kw: Any) -> Answer:
    """Разбор готового stdout: декодер кормится теми же байтами, что из процесса."""
    collector = TextCollector(max_chars=1_000_000, limit_rows=None, header_lines=0)
    decoder = SandboxFrameDecoder("doc", collector)
    decoder.feed(stdout.encode("utf-8"))
    return decoder.finish(_outcome(stdout, **kw), schema)


class TestDecode:
    """Всё, что отклоняется от контракта, — ошибка, а не частичный результат."""

    def test_trailer_line_is_parsed(self) -> None:
        line = SandboxPayload.encode(Answer(text="привет", pages=2))
        answer = _decode(line + "\n", Answer)
        assert (answer.text, answer.pages) == ("привет", 2)

    def test_free_output_around_trailer_is_ignored(self) -> None:
        line = SandboxPayload.encode(Answer(text="ok", pages=1))
        stdout = f"загружаю\n{line}\nготово\n"
        assert _decode(stdout, Answer).pages == 1

    def test_data_chunks_reach_the_sink(self) -> None:
        """Кадры уходят потребителю, трейлер данных не несёт."""
        collector = TextCollector(max_chars=1000, limit_rows=None, header_lines=0)
        decoder = SandboxFrameDecoder("doc", collector)
        chunks = "".join(
            [
                SandboxPayload.encode_chunk("первый ") + "\n",
                SandboxPayload.encode_chunk("второй") + "\n",
            ]
        )
        stdout = chunks + SandboxPayload.encode(Answer(text="", pages=2)) + "\n"
        decoder.feed(stdout.encode("utf-8"))
        answer = decoder.finish(_outcome(stdout), Answer)
        assert collector.text() == "первый второй"
        assert answer.pages == 2

    def test_chunks_arrive_split_across_reads(self) -> None:
        """Границы чтения из пайпа не совпадают с границами строк."""
        collector = TextCollector(max_chars=1000, limit_rows=None, header_lines=0)
        decoder = SandboxFrameDecoder("doc", collector)
        stdout = (
            SandboxPayload.encode_chunk("данные") + "\n"
            + SandboxPayload.encode(Answer(text="", pages=1)) + "\n"
        )
        raw = stdout.encode("utf-8")
        for start in range(0, len(raw), 7):
            decoder.feed(raw[start : start + 7])
        answer = decoder.finish(_outcome(stdout), Answer)
        assert collector.text() == "данные"
        assert answer.pages == 1

    def test_chunk_without_sink_is_error(self) -> None:
        """Trailer-only вызов не вправе получать кадры данных."""
        decoder = SandboxFrameDecoder("doc", NoChunks())
        with pytest.raises(LauncherError, match="unexpected data chunk"):
            decoder.feed((SandboxPayload.encode_chunk("данные") + "\n").encode())

    def test_declared_failure_has_no_traceback(self) -> None:
        """Ожидаемый отказ: текст payload'а без хвоста stderr и кода возврата."""
        stdout = SandboxPayload.encode_error("bad_format", "формат .md не читается")
        with pytest.raises(PayloadFailureError) as failure:
            _decode(
                stdout + "\n",
                Answer,
                exit_code=1,
                stderr="payload-error: bad_format: формат .md не читается",
            )
        assert str(failure.value) == "формат .md не читается"
        assert failure.value.kind == "bad_format"

    def test_declared_failure_keeps_limit_diagnostic(self) -> None:
        """Объяснение упёршегося лимита не теряется: трейсбека в нём нет."""
        stdout = SandboxPayload.encode_error("document_unreadable", "не разобрать")
        outcome = SandboxOutcome(
            "doc",
            RunResult(
                exit_code=1,
                stdout=stdout,
                stderr="",
                truncated_stdout=False,
                truncated_stderr=False,
                duration_ms=1,
                timed_out=False,
            ),
            "упёрлись в max_memory_bytes",
        )
        collector = TextCollector(max_chars=1000, limit_rows=None, header_lines=0)
        decoder = SandboxFrameDecoder("doc", collector)
        decoder.feed((stdout + "\n").encode("utf-8"))
        with pytest.raises(PayloadFailureError, match="упёрлись в max_memory_bytes"):
            decoder.finish(outcome, Answer)

    def test_failure_wins_over_exit_code(self) -> None:
        """Кадр ошибки объясняет отказ лучше, чем «exited with code 1»."""
        stdout = SandboxPayload.encode_error("unreadable", "файл не открыть")
        with pytest.raises(PayloadFailureError):
            _decode(stdout + "\n", Answer, exit_code=1, stderr="Traceback: шум")

    def test_two_failure_frames_are_error(self) -> None:
        line = SandboxPayload.encode_error("k", "m")
        with pytest.raises(SandboxPayloadError, match=r"2 .* lines"):
            _decode(f"{line}\n{line}\n", Answer, exit_code=1)

    def test_broken_failure_frame_is_error(self) -> None:
        with pytest.raises(SandboxPayloadError, match="error frame is not valid JSON"):
            _decode("sandbox-error:{битый\n", Answer, exit_code=1)

    def test_failure_frame_without_kind_is_error(self) -> None:
        stdout = 'sandbox-error:{"message": "текст"}\n'
        with pytest.raises(
            SandboxPayloadError, match="error frame does not match contract"
        ):
            _decode(stdout, Answer, exit_code=1)

    def test_missing_trailer_is_error(self) -> None:
        with pytest.raises(SandboxPayloadError, match="no 'sandbox-result:' line"):
            _decode("просто текст\n", Answer)

    def test_two_trailers_are_error(self) -> None:
        line = SandboxPayload.encode(Answer(text="ok", pages=1))
        with pytest.raises(SandboxPayloadError, match=r"2 .* lines"):
            _decode(f"{line}\n{line}\n", Answer)

    def test_chunk_after_trailer_is_error(self) -> None:
        """Кадр после трейлера означает, что поток собран не полностью."""
        stdout = (
            SandboxPayload.encode(Answer(text="ok", pages=1)) + "\n"
            + SandboxPayload.encode_chunk("лишнее") + "\n"
        )
        with pytest.raises(SandboxPayloadError, match="chunk after trailer"):
            _decode(stdout, Answer)

    def test_timeout_is_error(self) -> None:
        with pytest.raises(SandboxPayloadError, match="timed out"):
            _decode("", Answer, timed_out=True)

    def test_nonzero_exit_is_error(self) -> None:
        line = SandboxPayload.encode(Answer(text="ok", pages=1))
        with pytest.raises(SandboxPayloadError, match="exited with code 3"):
            _decode(line + "\n", Answer, exit_code=3, stderr="упал")

    def test_stderr_is_shown_in_error(self) -> None:
        with pytest.raises(SandboxPayloadError, match="нет файла"):
            _decode("", Answer, exit_code=1, stderr="Traceback: нет файла")

    def test_broken_json_is_error(self) -> None:
        with pytest.raises(SandboxPayloadError, match="not valid JSON"):
            _decode("sandbox-result:{текст\n", Answer)

    def test_schema_mismatch_is_error(self) -> None:
        with pytest.raises(SandboxPayloadError, match="does not match Answer"):
            _decode('sandbox-result:{"text": "ok"}\n', Answer)

    def test_plain_json_without_marker_is_error(self) -> None:
        """Маркер обязателен: иначе результат не отличить от вывода команды."""
        with pytest.raises(SandboxPayloadError, match="no 'sandbox-result:' line"):
            _decode('{"text": "ok", "pages": 1}\n', Answer)


_PAYLOAD = """
import json, sys
from pathlib import Path

request = json.loads(sys.stdin.read())
print("payload: работаю", file=sys.stderr)
text = Path(request["path"]).read_text(encoding="utf-8")
print("sandbox-chunk:" + json.dumps(text, ensure_ascii=False))
print("sandbox-result:" + json.dumps({"pages": 1}, ensure_ascii=False))
"""


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap не установлен")
@pytest.mark.skipif(os.geteuid() == 0, reason="под root userns ведёт себя иначе")
class TestCallStream:
    """Payload реально исполняется внутри песочницы и стримит результат."""

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

    def test_request_and_stream_travel_through_stdin_stdout(
        self, tmp_path: Path
    ) -> None:
        doc = tmp_path / "payload" / "doc.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("содержимое", encoding="utf-8")
        caller = self._caller(tmp_path, _PAYLOAD)
        collector = TextCollector(
            max_chars=1_000_000, limit_rows=None, header_lines=0
        )
        trailer = caller.call_stream(
            ["python3", "/opt/payload/main.py"],
            Request(path="/opt/payload/doc.txt"),
            collector,
            Trailer,
        )
        assert (collector.text(), trailer.pages) == ("содержимое", 1)

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
        answer = caller.call_stream(
            ["python3", "/opt/payload/main.py"], Request(path=""), NoChunks(), Answer
        )
        assert answer.text == "OSError"

    def test_payload_crash_is_reported(self, tmp_path: Path) -> None:
        script = "raise SystemExit('нет такого файла')\n"
        caller = self._caller(tmp_path, script)
        with pytest.raises(SandboxPayloadError, match="нет такого файла"):
            caller.call_stream(
                ["python3", "/opt/payload/main.py"],
                Request(path=""),
                NoChunks(),
                Answer,
            )

    def test_payload_timeout_is_reported(self, tmp_path: Path) -> None:
        script = "import time\ntime.sleep(30)\n"
        caller = self._caller(tmp_path, script, timeout_sec=1)
        with pytest.raises(SandboxPayloadError, match="timed out"):
            caller.call_stream(
                ["python3", "/opt/payload/main.py"],
                Request(path=""),
                NoChunks(),
                Answer,
            )

    def test_oversized_line_is_reported(self, tmp_path: Path) -> None:
        """Данные обязаны ехать кадрами: одна гигантская строка — нарушение."""
        script = (
            "import json\n"
            "print('sandbox-result:' + json.dumps("
            "{'text': 'x' * 8_000_000, 'pages': 1}))\n"
        )
        caller = self._caller(tmp_path, script)
        with pytest.raises(SandboxPayloadError, match="output line exceeds"):
            caller.call_stream(
                ["python3", "/opt/payload/main.py"],
                Request(path=""),
                NoChunks(),
                Answer,
            )

    def test_entry_must_not_be_empty(self, tmp_path: Path) -> None:
        caller = self._caller(tmp_path, _PAYLOAD)
        with pytest.raises(ValueError, match="entry must not be empty"):
            caller.call_stream([], Request(path=""), NoChunks(), Answer)
