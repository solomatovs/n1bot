"""Agent Templates

шаблоны используемые агентом для сборки запросов и сообщений.

подстановки отмечены `{имя}`, остальное отдаётся как есть.

Ошибки:
AgentTextError — файла текста нет в данных пакета или он пуст.
"""

from __future__ import annotations

from enum import StrEnum
from importlib import resources
from importlib.abc import Traversable
from typing import ClassVar

__all__ = ["AgentTemplate", "AgentTemplateFile", "AgentTextError"]


class AgentTextError(Exception):
    """Текст не найден среди данных пакета или пуст."""


class AgentTemplateFile(StrEnum):
    """Файлы текстов относительно каталога texts пакета."""

    SUMMARIZE_INSTRUCTION = "compact/summarize_instruction.txt"
    SUMMARY_WRAPPER = "compact/summary_wrapper.txt"
    INTERRUPTED_TOOL_RESULT = "compact/interrupted_tool_result.txt"
    NO_RESPONSE_REQUESTED = "compact/no_response_requested.txt"
    OUTPUT_LIMIT_HIT = "compact/output_limit_hit.txt"
    PROMPT_TOO_LONG = "compact/prompt_too_long.txt"

    SYSTEM_REMINDER = "attachments/system_reminder.txt"
    ENVIRONMENT = "attachments/environment.txt"
    ENVIRONMENT_UPDATE = "attachments/environment_update.txt"
    MODEL_NAMED = "attachments/model_named.txt"
    MODEL_PLAIN = "attachments/model_plain.txt"
    DATE = "attachments/date.txt"
    DATE_CHANGED = "attachments/date_changed.txt"
    TOOLS_ADDED = "attachments/tools_added.txt"
    TOOLS_REMOVED = "attachments/tools_removed.txt"
    TOOLS_AMBIENT = "attachments/tools_ambient.txt"
    SKILL_LISTING = "attachments/skill_listing.txt"
    QUEUED_HUMAN = "attachments/queued_human.txt"
    QUEUED_NOTIFICATION = "attachments/queued_notification.txt"
    QUEUED_NOTIFICATION_WITH_USER = "attachments/queued_notification_with_user.txt"


class AgentTemplate:
    """Чтение текстов из данных пакета boba.agent с кэшем на процесс."""

    PACKAGE: ClassVar[str] = "boba.agent"
    ROOT: ClassVar[str] = "template"
    ENCODING: ClassVar[str] = "utf-8"

    def __init__(self) -> None:
        self._root: Traversable = resources.files(self.PACKAGE) / self.ROOT
        self._cache: dict[AgentTemplateFile, str] = {}

    def read(self, tpl: AgentTemplateFile) -> str:
        if tpl in self._cache:
            return self._cache[tpl]

        source = self._root / tpl.value
        if not source.is_file():
            msg = f"agent text {tpl.value}: file is missing under {self._root}"
            raise AgentTextError(msg)

        body = source.read_text(encoding=self.ENCODING)
        if not body:
            msg = f"agent text {tpl.value}: file under {self._root} is empty"
            raise AgentTextError(msg)

        self._cache[tpl] = body

        return body
