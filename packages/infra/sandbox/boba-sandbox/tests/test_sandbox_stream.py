"""Тап живого вывода: окно наполняется по ходу работы процесса в песочнице."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from boba.sandbox import SandboxCaller, SandboxProfile
from boba.toolkit.launcher import TextCollector
from boba.toolkit.stream import ToolStreamBuffer, ToolStreamTap

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


class Request(BaseModel):
    """Запрос payload'а в тестах."""

    path: str


class Trailer(BaseModel):
    """Схема трейлера payload'а в тестах."""

    pages: int


_STREAM_PAYLOAD = """
import json, sys

request = json.loads(sys.stdin.read())
print("payload: работаю", file=sys.stderr)
print("sandbox-chunk:" + json.dumps("кусок данных", ensure_ascii=False))
print("sandbox-result:" + json.dumps({"pages": 1}, ensure_ascii=False))
"""


@pytest.fixture(autouse=True)
def clean_tap() -> Any:
    yield
    ToolStreamTap.set(None)


def _window(window_bytes: int = 64 * 1024) -> ToolStreamBuffer:
    def wake() -> None:
        pass

    return ToolStreamBuffer(window_bytes, wake)


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap не установлен")
@pytest.mark.skipif(os.geteuid() == 0, reason="под root userns ведёт себя иначе")
class TestCallTextTap:
    """Текстовый запуск: окно видит сырые stdout и stderr, результат полон."""

    @staticmethod
    def _caller(**profile_kw: Any) -> SandboxCaller:
        profile = SandboxProfile.model_validate({**_PROFILE_BASE, **profile_kw})
        return SandboxCaller("bash", profile, dict)

    def test_window_gets_both_streams_and_result_is_kept(self) -> None:
        buffer = _window()
        ToolStreamTap.set(buffer)

        outcome = self._caller().call_text("echo привет; echo беда >&2", stdin="")

        window = buffer.snapshot()
        assert "привет" in window.text
        assert "беда" in window.text
        assert "привет" in outcome.result.stdout
        assert "беда" in outcome.result.stderr

    def test_without_tap_nothing_changes(self) -> None:
        ToolStreamTap.set(None)

        outcome = self._caller().call_text("echo одинокий", stdin="")

        assert "одинокий" in outcome.result.stdout

    def test_window_stays_bounded_on_huge_output(self) -> None:
        """Мегабайты вывода не оседают в памяти: окно держит только хвост.

        Результат для LLM обрезается потолком профиля по началу, а окно
        продолжает ехать до конца процесса — в нём последние строки.
        """
        window_bytes = 64 * 1024
        buffer = _window(window_bytes)
        ToolStreamTap.set(buffer)

        # ~1.6 МБ: 200000 строк по 8 байт
        outcome = self._caller(max_output_bytes=window_bytes).call_text(
            "seq -w 1 200000", stdin=""
        )

        window = buffer.snapshot()
        assert len(window.text.encode()) <= window_bytes
        assert window.dropped_bytes > 1_000_000
        assert "200000" in window.text
        assert "0000001" not in window.text
        assert outcome.result.truncated_stdout is True

    def test_window_fills_while_the_process_runs(self) -> None:
        """Пробуждения приходят по ходу процесса, а не одним махом в конце."""
        sizes: list[int] = []
        holder: list[ToolStreamBuffer] = []

        def wake() -> None:
            sizes.append(len(holder[0].snapshot().text))

        buffer = ToolStreamBuffer(64 * 1024, wake)
        holder.append(buffer)
        ToolStreamTap.set(buffer)

        self._caller().call_text("echo старт; sleep 0.3; echo финиш", stdin="")

        assert len(sizes) >= 2
        assert sizes == sorted(sizes)
        assert sizes[0] < sizes[-1]


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap не установлен")
@pytest.mark.skipif(os.geteuid() == 0, reason="под root userns ведёт себя иначе")
class TestCallStreamTap:
    """Потоковый запуск: окно видит декодированные кадры, а не протокол."""

    @staticmethod
    def _caller(tmp_path: Path, script: str) -> SandboxCaller:
        payload_dir = tmp_path / "payload"
        payload_dir.mkdir(parents=True, exist_ok=True)
        (payload_dir / "main.py").write_text(script, encoding="utf-8")
        profile = SandboxProfile.model_validate(
            {
                **_PROFILE_BASE,
                "ro_binds": (
                    *_PROFILE_BASE["ro_binds"],
                    f"{payload_dir}:/opt/payload",
                ),
            }
        )
        return SandboxCaller("doc", profile, dict)

    def test_window_gets_chunks_and_stderr_lines(self, tmp_path: Path) -> None:
        buffer = _window()
        ToolStreamTap.set(buffer)
        collector = TextCollector(max_chars=1_000_000, limit_rows=None, header_lines=0)

        trailer = self._caller(tmp_path, _STREAM_PAYLOAD).call_stream(
            ["python3", "/opt/payload/main.py"],
            Request(path=""),
            collector,
            Trailer,
        )

        window = buffer.snapshot()
        assert "кусок данных" in window.text
        assert "payload: работаю" in window.text
        assert "sandbox-chunk" not in window.text
        assert (collector.text(), trailer.pages) == ("кусок данных", 1)
