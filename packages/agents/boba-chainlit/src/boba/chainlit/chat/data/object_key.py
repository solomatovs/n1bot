"""Адресация вложений: storage-ключ, путь в песочнице, ссылка и её маршрут."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import ClassVar, Self

from boba.sandbox import WORKSPACE_MOUNT

__all__ = ["AttachmentLinks", "AttachmentUrl", "ObjectKey"]


@dataclass(frozen=True, slots=True)
class ObjectKey:
    """Адрес вложения: ключ хранилища и путь того же файла в песочнице."""

    user_id: str
    thread_id: str
    name: str

    SEPARATOR: ClassVar[str] = "/"
    UPLOAD_DIR: ClassVar[str] = "upload"
    SEGMENTS: ClassVar[int] = 4
    THREAD_SEGMENTS: ClassVar[int] = 3
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

    @classmethod
    def from_workspace(cls, user_id: object, thread_id: object, path: str) -> Self:
        """Ключ по пути из песочницы; вне каталога вложений треда — ValueError."""
        rel = path.strip()

        mount = cls.SEPARATOR.join((WORKSPACE_MOUNT, ""))
        if rel.startswith(mount):
            rel = rel[len(mount) :]

        parts = rel.split(cls.SEPARATOR)
        if len(parts) != cls.THREAD_SEGMENTS:
            raise ValueError(cls._outside(thread_id, path))

        owner, upload_dir, name = parts
        if owner != str(thread_id):
            raise ValueError(cls._outside(thread_id, path))

        if upload_dir != cls.UPLOAD_DIR:
            raise ValueError(cls._outside(thread_id, path))

        if name in ("", ".", ".."):
            raise ValueError(cls._outside(thread_id, path))

        return cls(user_id=str(user_id), thread_id=str(thread_id), name=name)

    @classmethod
    def _outside(cls, thread_id: object, path: str) -> str:
        """Текст ошибки называет ожидаемый путь."""
        name = os.path.basename(path.strip())
        if not name:
            name = "<file name>"

        expected = cls.SEPARATOR.join(
            (WORKSPACE_MOUNT, str(thread_id), cls.UPLOAD_DIR, name)
        )
        return (
            f"file is outside the thread attachments dir: {path!r}; "
            f"expected {expected!r}"
        )

    def render(self) -> str:
        return self.SEPARATOR.join((self.user_id, self.in_thread()))

    def in_thread(self) -> str:
        """Путь файла внутри образа пользователя."""
        return self.SEPARATOR.join((self.thread_id, self.UPLOAD_DIR, self.name))

    def in_workspace(self) -> str:
        """Путь файла так, как его видит песочница."""
        return self.SEPARATOR.join((WORKSPACE_MOUNT, self.in_thread()))

    @classmethod
    def safe_name(cls, name: object, element_id: object) -> str:
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


@dataclass(frozen=True, slots=True)
class AttachmentUrl:
    """Адресация вложения по треду и id элемента — не по пути в хранилище."""

    thread_id: str
    element_id: str

    ROUTE: ClassVar[str] = "/attachment/{thread_id}/{element_id}"
    """Общий шаблон route и ссылки."""

    def path(self) -> str:
        return self.ROUTE.format(thread_id=self.thread_id, element_id=self.element_id)


@dataclass(frozen=True, slots=True)
class AttachmentLinks:
    """Выдаёт публичные ссылки на вложения; url-префикс знает только он."""

    prefix: str

    def url(self, thread_id: object, element_id: object) -> str:
        path = AttachmentUrl(str(thread_id), str(element_id)).path()
        return self.prefix.rstrip("/") + path
