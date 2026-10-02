"""Строгий размер пайпа канала: ровно запрошенный буфер либо отказ."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from boba.toolkit.launcher import LauncherError
from boba.toolkit.pump import PipePlumbing

F_GETPIPE_SZ = 1032


def _pipe() -> tuple[int, int]:
    read_fd, write_fd = os.pipe()
    return read_fd, write_fd


class TestRequire:
    def test_requested_size_is_set(self) -> None:
        read_fd, write_fd = _pipe()
        try:
            PipePlumbing.require(write_fd, 131072, "orders")

            assert fcntl.fcntl(write_fd, F_GETPIPE_SZ) >= 131072
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_zero_means_soft_widen(self) -> None:
        read_fd, write_fd = _pipe()
        try:
            PipePlumbing.require(write_fd, 0, "orders")
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_size_above_kernel_limit_is_refused_loudly(self) -> None:
        """Запрос выше fs.pipe-max-size непривилегированному не дают: отказ
        обязан назвать канал, размер и ручку ядра, а не промолчать."""
        limit = int(Path("/proc/sys/fs/pipe-max-size").read_text())

        read_fd, write_fd = _pipe()
        try:
            with pytest.raises(LauncherError, match=r"orders.*pipe-user-pages-soft"):
                PipePlumbing.require(write_fd, limit * 2, "orders")
        finally:
            os.close(read_fd)
            os.close(write_fd)
