"""Реестр ключей словарей chainlit: единые имена вместо строк по коду.

Chainlit отдаёт TypedDict-словари; ключом pyright принимает только строковый
литерал, поэтому реестр — Final-литералы, а не StrEnum.
"""

from __future__ import annotations

from typing import Final

__all__ = ["ElementField", "FileField", "StepField", "ThreadField"]


class ThreadField:
    """Ключи ThreadDict."""

    ID: Final = "id"
    STEPS: Final = "steps"
    ELEMENTS: Final = "elements"


class StepField:
    """Ключи StepDict."""

    ID: Final = "id"
    NAME: Final = "name"
    OUTPUT: Final = "output"
    IS_ERROR: Final = "isError"
    FEEDBACK: Final = "feedback"


class ElementField:
    """Ключи ElementDict."""

    ID: Final = "id"
    THREAD_ID: Final = "threadId"
    FOR_ID: Final = "forId"
    TYPE: Final = "type"
    CHAINLIT_KEY: Final = "chainlitKey"
    NAME: Final = "name"
    DISPLAY: Final = "display"
    SIZE: Final = "size"
    LANGUAGE: Final = "language"
    PAGE: Final = "page"
    PROPS: Final = "props"
    MIME: Final = "mime"
    URL: Final = "url"


class FileField:
    """Ключи FileDict загруженного файла."""

    NAME: Final = "name"
    TYPE: Final = "type"
