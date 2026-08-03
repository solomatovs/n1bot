"""Ссылка на сохранённое вложение: единственное место сборки её пути."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

__all__ = ["AttachmentLinks", "AttachmentUrl"]


@dataclass(frozen=True, slots=True)
class AttachmentUrl:
    """Адресация вложения по треду и id элемента — не по пути в хранилище."""

    thread_id: str
    element_id: str

    ROUTE: ClassVar[str] = "/attachment/{thread_id}/{element_id}"
    """Общий шаблон route и ссылки."""

    def path(self) -> str:
        return self.ROUTE.format(
            thread_id=self.thread_id, element_id=self.element_id
        )


@dataclass(frozen=True, slots=True)
class AttachmentLinks:
    """Выдаёт публичные ссылки на вложения; url-префикс знает только он."""

    prefix: str

    def url(self, thread_id: object, element_id: object) -> str:
        path = AttachmentUrl(str(thread_id), str(element_id)).path()
        return self.prefix.rstrip("/") + path
