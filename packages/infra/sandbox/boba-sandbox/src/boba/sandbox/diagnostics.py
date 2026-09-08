"""Перевод смерти вызова в объяснение: ядро убивает сигналом, но не говорит,
чей это лимит; здесь код возврата сопоставляется с лимитом профиля."""

from __future__ import annotations

from enum import IntEnum

from boba.sandbox.profile import SandboxProfile
from boba.toolkit.launcher import RunResult

__all__ = ["KilledBy", "SandboxDiagnostics"]


class KilledBy(IntEnum):
    """Коды смерти по сигналу в соглашении shell (128 + номер): гость отдаёт
    их на каждом уровне форков (WaitStatus), bash-тул — за свою команду."""

    SIGABRT = 134
    SIGKILL = 137
    SIGXCPU = 152
    SIGXFSZ = 153


class SandboxDiagnostics:
    """Сопоставляет код возврата процесса вызова с лимитом профиля.

    Только сигналы и таймаут: обычная ошибка команды с её кодом и stderr
    объяснения не требует, модель читает её как есть.
    """

    @classmethod
    def explain(cls, result: RunResult, profile: SandboxProfile) -> str:
        """Пустая строка — смерти, объяснимой лимитами песочницы, не было."""
        checks = (cls._timeout, cls._cpu, cls._file_size, cls._memory)
        for check in checks:
            message = check(result, profile)
            if message:
                return message

        return ""

    @classmethod
    def _timeout(cls, result: RunResult, profile: SandboxProfile) -> str:
        if not result.timed_out:
            return ""

        if profile.limits.timeout_sec is None:
            return ""

        return (
            f"Command aborted by the profile timeout: timeout_sec="
            f"{profile.limits.timeout_sec}s. Split the work into smaller steps or ask "
            f"an administrator to raise timeout_sec."
        )

    @classmethod
    def _cpu(cls, result: RunResult, profile: SandboxProfile) -> str:
        if result.exit_code != KilledBy.SIGXCPU:
            return ""

        return (
            f"CPU time limit reached: "
            f"process_cpu_sec={profile.limits.process_cpu_sec}s, "
            f"the process was killed by SIGXCPU. Optimise the computation or "
            f"process the data in chunks."
        )

    @classmethod
    def _file_size(cls, result: RunResult, profile: SandboxProfile) -> str:
        if result.exit_code != KilledBy.SIGXFSZ:
            return ""

        return (
            f"File size limit exceeded: process_file_bytes="
            f"{profile.limits.process_file_bytes} bytes, the write was aborted by "
            f"SIGXFSZ. Write a smaller file or split it into parts."
        )

    @classmethod
    def _memory(cls, result: RunResult, profile: SandboxProfile) -> str:
        """process_memory_bytes — RLIMIT_AS: парсеры документов резервируют гигабайты
        адресного пространства независимо от размера файла (pdfium ~2.3 ГБ)."""
        if result.exit_code not in (KilledBy.SIGKILL, KilledBy.SIGABRT):
            return ""

        limit = (
            f"process_memory_bytes={profile.limits.process_memory_bytes} bytes "
            f"(RLIMIT_AS, per process)"
        )
        if profile.limits.group_memory_bytes is not None:
            limit += (
                f" or group_memory_bytes={profile.limits.group_memory_bytes} bytes "
                f"(cgroup memory.max, the whole run)"
            )

        return (
            f"Memory limit reached: {limit}. Stream the data "
            f"instead of loading all of it into memory, or ask an "
            f"administrator to raise the limit: document parsers reserve "
            f"gigabytes of address space regardless of file size."
        )
