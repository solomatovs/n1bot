"""Опции лаунчера; отдельный stdlib-модуль вне import-цепочки `python -m launcher`."""

from __future__ import annotations

import resource
from dataclasses import dataclass

__all__ = ["LauncherOptions", "ResourceLimits"]


@dataclass(frozen=True)
class LauncherOptions:
    """Тайминги и размеры лаунчера."""

    mount_wait_sec: float = 10.0
    mount_poll_sec: float = 0.05
    shutdown_wait_sec: float = 5.0
    copy_chunk_bytes: int = 1 << 20


@dataclass(frozen=True)
class ResourceLimits:
    """Лимиты команды: RLIMIT_AS/CPU/FSIZE/NOFILE; 0 — не выставлять."""

    max_memory_bytes: int = 0
    max_cpu_sec: int = 0
    max_file_size_bytes: int = 0
    max_open_files: int = 0

    def apply_to_current_process(self) -> None:
        if self.max_memory_bytes:
            memory = (self.max_memory_bytes, self.max_memory_bytes)
            resource.setrlimit(resource.RLIMIT_AS, memory)
        if self.max_cpu_sec:
            cpu = (self.max_cpu_sec, self.max_cpu_sec)
            resource.setrlimit(resource.RLIMIT_CPU, cpu)
        if self.max_file_size_bytes:
            fsize = (self.max_file_size_bytes, self.max_file_size_bytes)
            resource.setrlimit(resource.RLIMIT_FSIZE, fsize)
        if self.max_open_files:
            nofile = (self.max_open_files, self.max_open_files)
            resource.setrlimit(resource.RLIMIT_NOFILE, nofile)

    def apply_to_process(self, pid: int) -> None:
        """prlimit из родителя: не требует preexec_fn, безопасен при потоках."""
        if self.max_memory_bytes:
            memory = (self.max_memory_bytes, self.max_memory_bytes)
            resource.prlimit(pid, resource.RLIMIT_AS, memory)
        if self.max_cpu_sec:
            cpu = (self.max_cpu_sec, self.max_cpu_sec)
            resource.prlimit(pid, resource.RLIMIT_CPU, cpu)
        if self.max_file_size_bytes:
            fsize = (self.max_file_size_bytes, self.max_file_size_bytes)
            resource.prlimit(pid, resource.RLIMIT_FSIZE, fsize)
        if self.max_open_files:
            nofile = (self.max_open_files, self.max_open_files)
            resource.prlimit(pid, resource.RLIMIT_NOFILE, nofile)
