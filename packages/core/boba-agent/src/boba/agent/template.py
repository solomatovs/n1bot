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
    REQUEST_INTERRUPTED = "compact/request_interrupted.txt"
    REQUEST_INTERRUPTED_TOOL_USE = "compact/request_interrupted_tool_use.txt"
    TOOL_RESULT_MISSING = "compact/tool_result_missing.txt"
    NO_CONTENT = "compact/no_content.txt"
    NO_MESSAGE_CONTENT = "compact/no_message_content.txt"
    THRASHING = "compact/thrashing.txt"
    COMPACTION_FAILED = "compact/compaction_failed.txt"

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

    USER_REJECTED = "tools/user_rejected.txt"
    USER_REJECTED_SAID = "tools/user_rejected_said.txt"
    USER_REJECTED_RESULT = "tools/user_rejected_result.txt"
    TURN_ABORTED = "tools/turn_aborted.txt"
    NOT_GRANTED = "tools/not_granted.txt"
    PLAN_MODE = "tools/plan_mode.txt"
    CLASSIFIER_DENIED = "tools/classifier_denied.txt"
    CLASSIFIER_FAILED = "tools/classifier_failed.txt"
    NO_SUCH_TOOL = "tools/no_such_tool.txt"
    NO_OUTPUT = "tools/no_output.txt"
    TRUNCATED = "tools/truncated.txt"
    ANSWERS = "tools/answers.txt"
    NO_ANSWERS = "tools/no_answers.txt"
    ASK_USER_QUESTION = "tools/ask_user_question.txt"
    SKILL = "tools/skill.txt"
    SKILL_LAUNCHED = "tools/skill_launched.txt"
    NO_SUCH_SKILL = "tools/no_such_skill.txt"


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
            msg = (
                f"The agent template '{tpl.value}' is missing from the package data "
                f"under {self._root}. The boba-agent package is installed incompletely."
            )
            raise AgentTextError(msg)

        body = source.read_text(encoding=self.ENCODING)
        if not body:
            msg = (
                f"The agent template '{tpl.value}' under {self._root} is empty, "
                "so the agent has no text to send. Restore the file from the package."
            )
            raise AgentTextError(msg)

        self._cache[tpl] = body

        return body
