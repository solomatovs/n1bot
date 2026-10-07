"""Приёмник отчётов о ходе работы для тестов тел инструментов.

Ошибки: своих не выпускает; отчёт, не проходящий модель ProgressReport, —
pydantic.ValidationError.
"""

from __future__ import annotations

import os
from typing import ClassVar

from boba.toolkit.ports import ProgressReport, ToolProgress

__all__ = ["HeardProgress"]


class HeardProgress:
    """Пайп отчётов о ходе работы: тело инструмента пишет через ToolProgress,
    тест читает всё, что тело сообщило.

    Создаётся тестом, который зовёт тело инструмента напрямую и проверяет,
    что видит пользователь, пока инструмент работает. progress отдаётся телу
    параметром; messages() закрывает конец записи и отдаёт тексты отчётов
    по порядку. Конец чтения закрывает close().
    """

    ENCODING: ClassVar[str] = "utf-8"
    READ_BYTES: ClassVar[int] = 65536

    def __init__(self) -> None:
        self._read_fd, self._write_fd = os.pipe()
        self._written = True
        self.progress = ToolProgress(self._write_fd)

    def messages(self) -> list[str]:
        """Тексты всех отчётов тела; придержанный частотой отчёт досылается."""
        texts: list[str] = []
        for report in self.reports():
            texts.append(report.message)

        return texts

    def reports(self) -> list[ProgressReport]:
        """Все отчёты тела; после вызова тело писать уже не может."""
        if self._written:
            self.progress.close()
            os.close(self._write_fd)
            self._written = False

        raw = b""
        while chunk := os.read(self._read_fd, self.READ_BYTES):
            raw += chunk

        found: list[ProgressReport] = []
        for line in raw.decode(self.ENCODING).splitlines():
            found.append(ProgressReport.model_validate_json(line))

        return found

    def close(self) -> None:
        if self._written:
            os.close(self._write_fd)
            self._written = False

        os.close(self._read_fd)
