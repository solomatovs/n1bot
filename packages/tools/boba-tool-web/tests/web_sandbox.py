"""Профиль песочницы web-узлов и локальный http-сервер для интеграционных тестов.

Сеть узлу нужна настоящая: страница скачивается из песочницы, поэтому профиль
идёт с network=true, а сервер поднимается на 127.0.0.1.
"""

from __future__ import annotations

import base64
import os
import shutil
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, ClassVar

import pytest

REPO = Path(__file__).resolve().parents[4]
SANDBOX = REPO / "build" / "src" / "sandbox"
ROOTFS = SANDBOX / "rootfs"

SRC_PACKAGES = (
    "core/boba-cancellation",
    "core/boba-toolkit",
    "infra/sandbox/boba-sandbox",
    "infra/transport/boba-transport-http",
    "infra/transport/boba-web",
    "tools/boba-tool-web",
)

needs_sandbox = pytest.mark.skipif(
    shutil.which("bwrap") is None or not (ROOTFS / "bin" / "sh").exists(),
    reason="нет bwrap или артефактов песочницы (собрать: make deps)",
)
needs_userns = pytest.mark.skipif(
    os.geteuid() == 0, reason="под root user namespace ведёт себя иначе"
)

PAGE = (
    "<html><body>"
    "<h1>Отчёт</h1>"
    "<p>первая строка</p>"
    "<p>вторая строка</p>"
    "<p>метка искомая</p>"
    "<p>четвёртая строка</p>"
    "</body></html>"
)


class Credentials:
    """Логин и пароль защищённой страницы тестового сервера."""

    USER: ClassVar[str] = "web-user"
    PASSWORD: ClassVar[str] = "s3cret-пароль"

    @classmethod
    def header(cls) -> str:
        raw = f"{cls.USER}:{cls.PASSWORD}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")


class PageHandler(BaseHTTPRequestHandler):
    """Три маршрута: открытая страница, страница под basic-auth и 404."""

    OPEN: ClassVar[str] = "/page"
    GUARDED: ClassVar[str] = "/private"

    def do_GET(self) -> None:
        if self.path == self.OPEN:
            self._send(200, PAGE)
            return

        if self.path == self.GUARDED:
            self._guarded()
            return

        self._send(404, "not found")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Сервер молчит: его лог тесту не нужен."""

    def _guarded(self) -> None:
        header = self.headers.get("Authorization", "")

        if header != Credentials.header():
            self._send(401, "unauthorized")
            return

        self._send(200, PAGE)

    def _send(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class SandboxLayout:
    """Монтирования и env: код пакетов кладётся поверх собранного site."""

    @staticmethod
    def ro_binds() -> list[str]:
        return [
            f"{SANDBOX / 'third' / 'python'}:/opt/python",
            f"{SANDBOX / 'site'}:/opt/site",
            f"{REPO / 'packages'}:/opt/src",
            "/etc/hosts:/etc/hosts",
            "/etc/resolv.conf:/etc/resolv.conf",
        ]

    @staticmethod
    def python_path() -> str:
        parts: list[str] = []
        for name in SRC_PACKAGES:
            parts.append(f"/opt/src/{name}/src")
        parts.append("/opt/site")
        return ":".join(parts)


def sandbox_profile(**kw: Any) -> dict[str, Any]:
    """Профиль web-узла: сеть включена, рабочий каталог — tmpfs."""
    profile: dict[str, Any] = {
        "rootfs": str(ROOTFS),
        "ro_binds": tuple(SandboxLayout.ro_binds()),
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
        "tmpfs": ("/tmp:256M",),  # noqa: S108
        "network": True,
        "env_set": {
            "PATH": "/opt/python/bin:/usr/local/bin:/usr/bin:/bin",
            "PYTHONHOME": "/opt/python",
            "PYTHONPATH": SandboxLayout.python_path(),
            "LD_LIBRARY_PATH": "/opt/python/lib",
            "HOME": "/tmp",  # noqa: S108
            "LANG": "C.UTF-8",
        },
        "timeout_sec": 60,
        "max_memory_bytes": 2 * 1024 * 1024 * 1024,
        "max_cpu_sec": 60,
        "max_file_size_bytes": 64 * 1024 * 1024,
        "max_open_files": 1024,
        "max_processes": 256,
        "max_output_bytes": 1 << 20,
        "cgroup_base": "",
        "oom_score_adj": 0,
        "cwd": "/tmp",  # noqa: S108
    }
    profile.update(kw)
    return profile
