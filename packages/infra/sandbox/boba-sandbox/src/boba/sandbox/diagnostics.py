"""Перевод сбоя песочницы в объяснение: ядро не говорит, чей это лимит."""

from __future__ import annotations

from typing import ClassVar

from boba.sandbox.process_runner import RunResult
from boba.sandbox.profile import SandboxProfile

__all__ = ["SandboxDiagnostics"]


class SandboxDiagnostics:
    """Сопоставляет код возврата и stderr с лимитом профиля."""

    SIGABRT_CODE: ClassVar[int] = 134
    SIGKILL_CODE: ClassVar[int] = 137
    SIGXCPU_CODE: ClassVar[int] = 152
    SIGXFSZ_CODE: ClassVar[int] = 153

    CPU_MARKERS: ClassVar[tuple[str, ...]] = (
        "CPU time limit exceeded",
        "cpu limit",
    )
    FSIZE_MARKERS: ClassVar[tuple[str, ...]] = (
        "File size limit exceeded",
        "File too large",
    )
    FD_MARKERS: ClassVar[tuple[str, ...]] = (
        "Too many open files",
        "EMFILE",
    )
    PROCESS_MARKERS: ClassVar[tuple[str, ...]] = (
        "fork: retry",
        "fork: Resource temporarily unavailable",
        "Resource temporarily unavailable",
        "can't fork",
        "BlockingIOError",
    )
    MEMORY_MARKERS: ClassVar[tuple[str, ...]] = (
        "MemoryError",
        "Cannot allocate memory",
        "out of memory",
        "Out of memory",
        "bad_alloc",
        # rust-аллокатор (pdfium/liteparse) при исчерпании RLIMIT_AS
        "memory allocation of",
        "failed to map segment from shared object",
    )
    SPACE_MARKERS: ClassVar[tuple[str, ...]] = ("No space left on device",)
    NETWORK_MARKERS: ClassVar[tuple[str, ...]] = (
        "Temporary failure in name resolution",
        "Name or service not known",
        "Network is unreachable",
        "nodename nor servname provided",
        "Could not resolve host",
    )

    @classmethod
    def explain(cls, result: RunResult, profile: SandboxProfile) -> str:
        """Пустая строка — сбоя, объяснимого лимитами песочницы, не найдено."""
        checks = (
            cls._timeout,
            cls._cpu,
            cls._file_size,
            cls._open_files,
            cls._processes,
            cls._memory,
            cls._space,
        )
        for check in checks:
            message = check(result, profile)
            if message:
                return message
        return cls._network(result, profile)

    @classmethod
    def _timeout(cls, result: RunResult, profile: SandboxProfile) -> str:
        if not result.timed_out:
            return ""
        return (
            f"Command aborted by the profile timeout: timeout_sec="
            f"{profile.timeout_sec}s. Split the work into smaller steps or ask "
            f"an administrator to raise timeout_sec."
        )

    @classmethod
    def _cpu(cls, result: RunResult, profile: SandboxProfile) -> str:
        by_signal = result.exit_code == cls.SIGXCPU_CODE
        by_text = cls._matched(result.stderr, cls.CPU_MARKERS)
        if not by_signal and not by_text:
            return ""
        return (
            f"CPU time limit reached: max_cpu_sec={profile.max_cpu_sec}s, "
            f"the process was killed by SIGXCPU. Optimise the computation or "
            f"process the data in chunks."
        )

    @classmethod
    def _file_size(cls, result: RunResult, profile: SandboxProfile) -> str:
        by_signal = result.exit_code == cls.SIGXFSZ_CODE
        by_text = cls._matched(result.stderr, cls.FSIZE_MARKERS)
        if not by_signal and not by_text:
            return ""
        return (
            f"File size limit exceeded: max_file_size_bytes="
            f"{profile.max_file_size_bytes} bytes, the write was aborted by "
            f"SIGXFSZ. Write a smaller file or split it into parts."
        )

    @classmethod
    def _open_files(cls, result: RunResult, profile: SandboxProfile) -> str:
        if not cls._matched(result.stderr, cls.FD_MARKERS):
            return ""
        return (
            f"Open file limit reached: max_open_files={profile.max_open_files}. "
            f"Close files right after use; the limit also counts "
            f"stdin/stdout/stderr."
        )

    @classmethod
    def _processes(cls, result: RunResult, profile: SandboxProfile) -> str:
        if not cls._matched(result.stderr, cls.PROCESS_MARKERS):
            return ""
        limit = f"max_processes={profile.max_processes}"
        if profile.cgroup_pids_max is not None:
            limit += f" or cgroup_pids_max={profile.cgroup_pids_max} for the whole run"
        return (
            f"Process limit reached: {limit}. "
            f"Start fewer parallel processes and pipelines."
        )

    @classmethod
    def _memory(cls, result: RunResult, profile: SandboxProfile) -> str:
        """max_memory_bytes — RLIMIT_AS: парсеры документов резервируют гигабайты
        адресного пространства независимо от размера файла (pdfium ~2.3 ГБ)."""
        by_signal = result.exit_code in (cls.SIGKILL_CODE, cls.SIGABRT_CODE)
        by_text = cls._matched(result.stderr, cls.MEMORY_MARKERS)
        if not by_signal and not by_text:
            return ""
        limit = (
            f"max_memory_bytes={profile.max_memory_bytes} bytes "
            f"(RLIMIT_AS, per process)"
        )
        if profile.cgroup_memory_bytes is not None:
            limit += (
                f" or cgroup_memory_bytes={profile.cgroup_memory_bytes} bytes "
                f"(cgroup memory.max, the whole run)"
            )
        return (
            f"Memory limit reached: {limit}. Stream the data "
            f"instead of loading all of it into memory, or ask an "
            f"administrator to raise the limit: document parsers reserve "
            f"gigabytes of address space regardless of file size."
        )

    @classmethod
    def _space(cls, result: RunResult, profile: SandboxProfile) -> str:
        if not cls._matched(result.stderr, cls.SPACE_MARKERS):
            return ""
        return (
            f"No space left in the working directory: its size is fixed by the "
            f"workspace image and does not grow. Delete unused files in "
            f"{profile.cwd}."
        )

    @classmethod
    def _network(cls, result: RunResult, profile: SandboxProfile) -> str:
        if profile.network:
            return ""
        if not cls._matched(result.stderr, cls.NETWORK_MARKERS):
            return ""
        return (
            "Network is disabled in this sandbox profile (network=false): DNS "
            "and outbound connections are unavailable; the command itself is "
            "not at fault. Pick a profile whose description allows network."
        )

    @staticmethod
    def _matched(text: str, markers: tuple[str, ...]) -> bool:
        for marker in markers:  # noqa: SIM110
            if marker in text:
                return True
        return False
