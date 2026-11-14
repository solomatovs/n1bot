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
    ACP_HISTORY = "acp/v2/history"
    ACP_STATE = "acp/v2/state"
    ACP_COMMANDS = "acp/v2/commands"
    ACP_OPTIONS = "acp/v2/options"
    ACP_NOTICE = "acp/v2/notice"
    ACP_PERMISSION = "acp/v2/permission"
    ACP_ELICITATION = "acp/v2/elicitation"
    ACP_V1_PROMPT = "acp/v1/prompt"
    ACP_V1_HISTORY = "acp/v1/history"
    ACP_V1_OPTIONS = "acp/v1/options"
    ACP_V1_COMMANDS = "acp/v1/commands"
    ACP_V1_NOTICE = "acp/v1/notice"
    ACP_V1_PERMISSION = "acp/v1/permission"
    ACP_V1_ELICITATION = "acp/v1/elicitation"


    @cache
    def text(self) -> LiteralString:
        """Прочитать SQL один раз; параметры передаются отдельно в AsyncRawCursor."""
        source = (
            files("boba.agent")
            .joinpath("sql", f"{self.value}.sql")
            .read_text(encoding="utf-8")
        )
        return cast(LiteralString, source)
