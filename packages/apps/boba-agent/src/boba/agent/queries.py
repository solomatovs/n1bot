"""SQL-файлы агента: неизменный текст запросов, общий для сервиса и администратора."""

from enum import StrEnum
from functools import cache
from importlib.resources import files
from typing import LiteralString, cast


class Query(StrEnum):
    """Рабочие запросы, поставляемые вместе с агентом."""

    LOCK = "lock"
    HEAD = "head"
    APPEND = "append"
    READ = "read"
    LATEST = "latest"
    COMPACT_STATE = "compact_state"
    SESSION_CREATE = "session_create"
    SESSION_GET = "session_get"
    SESSION_LIST = "session_list"
    OWNER_LOCK = "owner_lock"
    OWNER_UNLOCK = "owner_unlock"
    PENDING_INPUTS = "pending_inputs"
    PENDING_TASKS = "pending_tasks"
    TASK_ACKNOWLEDGEMENTS = "task_acknowledgements"
    HISTORY_INTEGRITY = "history_integrity"
    OPENAI = "openai"
    ACP_HISTORY = "acp_history"
    ACP_STATE = "acp_state"
    ACP_COMMANDS = "acp_commands"
    ACP_OPTIONS = "acp_options"
    ACP_NOTICE = "acp_notice"
    ACP_PERMISSION = "acp_permission"
    ACP_ELICITATION = "acp_elicitation"

    @cache
    def text(self) -> LiteralString:
        """Прочитать SQL один раз; параметры передаются отдельно в AsyncRawCursor."""
        source = (
            files("boba.agent")
            .joinpath("sql", f"{self.value}.sql")
            .read_text(encoding="utf-8")
        )
        return cast(LiteralString, source)
