"""Storage-ключ вложения: единственное место сборки и разбора пути."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import ClassVar, Self

__all__ = ["ObjectKey"]


@dataclass(frozen=True, slots=True)
class ObjectKey:
    """Путь вложения в хранилище: {user_id}/{thread_id}/upload/{name}.

    Лежит внутри рабочей папки чата, которую песочница монтирует на запись, —
    загруженные файлы доступны инструментам этого же чата. Путь вычисляется
    из идентификаторов и имени, в базе не хранится.
    """

    user_id: str
    thread_id: str
    name: str

    SEPARATOR: ClassVar[str] = "/"
    UPLOAD_DIR: ClassVar[str] = "upload"
    SEGMENTS: ClassVar[int] = 4
    MAX_NAME_BYTES: ClassVar[int] = 255
    """Предел ext4 на длину имени файла."""

    @classmethod
    def build(
        cls,
        user_id: object,
        thread_id: object,
        name: object,
        element_id: object,
    ) -> Self:
        return cls(
            user_id=str(user_id),
            thread_id=str(thread_id),
            name=cls.safe_name(name, element_id),
        )

    @classmethod
    def parse(cls, raw: str) -> Self:
        parts = raw.split(cls.SEPARATOR)
        if len(parts) != cls.SEGMENTS:
            raise ValueError(f"invalid object_key: {raw!r}")
        for part in parts:
            if part in ("", ".", ".."):
                raise ValueError(f"invalid object_key: {raw!r}")
        user_id, thread_id, upload_dir, name = parts
        if upload_dir != cls.UPLOAD_DIR:
            raise ValueError(f"invalid object_key: {raw!r}")
        return cls(user_id=user_id, thread_id=thread_id, name=name)

    def render(self) -> str:
        return self.SEPARATOR.join((self.user_id, self.in_thread()))

    def in_thread(self) -> str:
        """Путь вложения так, как его видит песочница внутри /workspace."""
        return self.SEPARATOR.join((self.thread_id, self.UPLOAD_DIR, self.name))

    @classmethod
    def safe_name(cls, name: object, element_id: object) -> str:
        """Имя от пользователя — один сегмент пути; иначе откат на id."""
        base = os.path.basename(str(name)).strip()
        kept: list[str] = []
        for char in base:
            if char in ("/", "\\"):
                continue
            if char.isprintable():
                kept.append(char)
        cleaned = "".join(kept).strip()
        if cleaned in ("", ".", ".."):
            return str(element_id)
        return cls._truncate(cleaned)

    @classmethod
    def _truncate(cls, name: str) -> str:
        encoded = name.encode("utf-8")
        if len(encoded) <= cls.MAX_NAME_BYTES:
            return name
        stem, dot, suffix = name.rpartition(".")
        tail = ""
        if dot:
            tail = "." + suffix[: cls.MAX_NAME_BYTES // 4]
        room = cls.MAX_NAME_BYTES - len(tail.encode("utf-8"))
        head = stem
        if not dot:
            head = name
        while len(head.encode("utf-8")) > room:
            head = head[:-1]
        return head + tail
