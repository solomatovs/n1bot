"""Реестр ключей словарей chainlit: единые имена вместо строк по коду.

Chainlit отдаёт TypedDict-словари; ключом pyright принимает только строковый
литерал, поэтому реестр — Final-литералы, а не StrEnum.
"""

from __future__ import annotations

from typing import Final

__all__ = ["FileField", "StepField", "ThreadField", "ThreadMetaField"]


class ThreadField:
    """Ключи ThreadDict."""

    ID: Final = "id"
    METADATA: Final = "metadata"
    STEPS: Final = "steps"


class ThreadMetaField:
    """Ключи metadata треда: их пишет persist сессии chainlit, сессию агента
    и профиль, под которым она открыта, — агент треда."""

    CHAT_PROFILE: Final = "chat_profile"
    AGENT_SESSION: Final = "agent_session"


class StepField:
    """Ключи StepDict."""

    ID: Final = "id"
    PARENT_ID: Final = "parentId"
    NAME: Final = "name"
    TYPE: Final = "type"
    OUTPUT: Final = "output"
    IS_ERROR: Final = "isError"
    FEEDBACK: Final = "feedback"
    METADATA: Final = "metadata"


class FileField:
    """Ключи FileDict загруженного файла."""

    NAME: Final = "name"
    TYPE: Final = "type"
