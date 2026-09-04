"""Ключи хранилища: каталоги треда и рабочего каталога, ключ объекта, свойства элемента.

Ошибки: своих не выпускает; неразборный ключ — ValueError.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from boba.toolkit.failure import ValidationText

__all__ = [
    "DirKey",
    "ElementProps",
    "KeyField",
    "KeySegment",
    "ObjectKey",
    "ThreadDir",
    "WorkspaceMount",
]


class ThreadDir(StrEnum):
    """Каталоги треда: вложения пользователя и спеки диаграмм."""

    UPLOAD = "upload"
    MERMAID = "mermaid"


class WorkspaceMount:
    """Точка рабочего каталога чата в песочнице; значение приходит из профиля.

    Ключи вложений строятся по ней, поэтому она нужна и вне запуска — в
    отрисовке и в инструментах. Настраивается один раз при загрузке
    инструментов; обращение до настройки — ошибка, а не тихий дефолт.
    """

    _PATH: ClassVar[str] = ""

    @classmethod
    def configure(cls, path: str) -> None:
        if not path:
            msg = "workspace mount is empty: sandbox profile must declare it"
            raise RuntimeError(msg)

        cls._PATH = path

    @classmethod
    def path(cls) -> str:
        if not cls._PATH:
            msg = (
                "workspace mount is not configured: load_tools() sets it from "
                "the sandbox profile"
            )
            raise RuntimeError(msg)

        return cls._PATH


class KeyField(StrEnum):
    """Поля ключа: ими же именуются сегменты при разборе строки."""

    USER_ID = "user_id"
    THREAD_ID = "thread_id"
    DIR = "dir"
    NAME = "name"


class KeySegment(BaseModel):
    """Общая валидация сегментов ключа: пустых и путей наружу здесь не бывает."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    SEPARATOR: ClassVar[str] = "/"

    @field_validator("*", mode="after")
    @classmethod
    def _segment_is_safe(cls, value: object) -> object:
        if not isinstance(value, str):
            return value

        if value in ("", ".", ".."):
            msg = f"invalid key segment {value!r}: expected a name, not '', '.' or '..'"
            raise ValueError(msg)

        if cls.SEPARATOR in value:
            msg = (
                f"invalid key segment {value!r}: "
                f"separator {cls.SEPARATOR!r} is not allowed inside a segment"
            )
            raise ValueError(msg)

        return value


class ObjectKey(KeySegment):
    """Адрес вложения: ключ хранилища и путь того же файла в песочнице."""

    SEGMENTS: ClassVar[int] = 4
    THREAD_SEGMENTS: ClassVar[int] = 3
    MAX_NAME_BYTES: ClassVar[int] = 255
    """Предел ext4 на длину имени файла."""

    user_id: str
    thread_id: str
    name: str
    dir: ThreadDir = ThreadDir.UPLOAD
    """Каталог треда: upload — вложения, mermaid — спеки диаграмм."""

    @classmethod
    def build(
        cls,
        user_id: object,
        thread_id: object,
        name: object,
        element_id: object,
        *,
        dir_thread: ThreadDir = ThreadDir.UPLOAD,
    ) -> Self:
        return cls(
            user_id=str(user_id),
            thread_id=str(thread_id),
            name=cls.safe_name(name, element_id),
            dir=dir_thread,
        )

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Ключ хранилища -> модель; проверки полей живут в самой модели."""
        parts = raw.split(cls.SEPARATOR)
        if len(parts) != cls.SEGMENTS:
            msg = (
                f"invalid object_key {raw!r}: expected {cls.SEGMENTS} segments "
                f"user_id/thread_id/dir/name, got {len(parts)}"
            )
            raise ValueError(msg)

        order = (KeyField.USER_ID, KeyField.THREAD_ID, KeyField.DIR, KeyField.NAME)
        fields = dict(zip(order, parts, strict=True))

        try:
            return cls.model_validate(fields)
        except ValidationError as e:
            msg = f"invalid object_key {raw!r}: {ValidationText.of(e)}"
            raise ValueError(msg) from e

    @classmethod
    def from_workspace(cls, user_id: object, thread_id: object, path: str) -> Self:
        """Ключ по пути из песочницы; вне каталогов треда — ValueError."""
        rel = path.strip()

        mount = cls.SEPARATOR.join((WorkspaceMount.path(), ""))
        if rel.startswith(mount):
            rel = rel[len(mount) :]

        parts = rel.split(cls.SEPARATOR)
        if len(parts) != cls.THREAD_SEGMENTS:
            raise ValueError(cls._outside(thread_id, path))

        order = (KeyField.THREAD_ID, KeyField.DIR, KeyField.NAME)
        fields: dict[str, str] = dict(zip(order, parts, strict=True))
        fields[KeyField.USER_ID] = str(user_id)

        try:
            key = cls.model_validate(fields)
        except ValidationError as e:
            raise ValueError(cls._outside(thread_id, path)) from e

        if key.thread_id != str(thread_id):
            raise ValueError(cls._outside(thread_id, path))

        return key

    @classmethod
    def _outside(cls, thread_id: object, path: str) -> str:
        """Текст ошибки называет ожидаемый путь."""
        name = os.path.basename(path.strip())
        if not name:
            name = "<file name>"

        dirs = "|".join(sorted(ThreadDir))
        expected = cls.SEPARATOR.join(
            (WorkspaceMount.path(), str(thread_id), f"{{{dirs}}}", name)
        )
        return (
            f"file is outside the thread attachments dir: {path!r}; "
            f"expected {expected!r}"
        )

    def render(self) -> str:
        return self.SEPARATOR.join((self.user_id, self.in_thread()))

    def in_thread(self) -> str:
        """Путь файла внутри образа пользователя."""
        return self.SEPARATOR.join((self.thread_id, self.dir, self.name))

    def in_workspace(self) -> str:
        """Путь файла так, как его видит песочница."""
        return self.SEPARATOR.join((WorkspaceMount.path(), self.in_thread()))

    def dir_key(self) -> DirKey:
        """Каталог, в котором лежит файл."""
        return DirKey(user_id=self.user_id, thread_id=self.thread_id, dir=self.dir)

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


class DirKey(KeySegment):
    """Адрес каталога треда: префикс в хранилище и путь того же каталога в песочнице."""

    SEGMENTS: ClassVar[int] = 3

    user_id: str
    thread_id: str
    dir: ThreadDir

    @classmethod
    def of(cls, user_id: object, thread_id: object, dir_thread: ThreadDir) -> Self:
        return cls(user_id=str(user_id), thread_id=str(thread_id), dir=dir_thread)

    @classmethod
    def parse(cls, raw: str) -> Self:
        """Префикс каталога -> модель; проверки полей живут в самой модели."""
        parts = raw.split(cls.SEPARATOR)
        if len(parts) != cls.SEGMENTS:
            msg = (
                f"invalid dir key {raw!r}: expected {cls.SEGMENTS} segments "
                f"user_id/thread_id/dir, got {len(parts)}"
            )
            raise ValueError(msg)

        order = (KeyField.USER_ID, KeyField.THREAD_ID, KeyField.DIR)
        fields = dict(zip(order, parts, strict=True))

        try:
            return cls.model_validate(fields)
        except ValidationError as e:
            msg = f"invalid dir key {raw!r}: {ValidationText.of(e)}"
            raise ValueError(msg) from e

    def render(self) -> str:
        return self.SEPARATOR.join((self.user_id, self.in_thread()))

    def in_thread(self) -> str:
        """Путь каталога внутри образа пользователя."""
        return self.SEPARATOR.join((self.thread_id, self.dir))

    def in_workspace(self) -> str:
        """Путь каталога так, как его видит песочница."""
        return self.SEPARATOR.join((WorkspaceMount.path(), self.in_thread()))

    def file(self, name: str) -> ObjectKey:
        """Ключ файла в этом каталоге; имя приходит из листинга, не от LLM."""
        return ObjectKey(
            user_id=self.user_id,
            thread_id=self.thread_id,
            name=name,
            dir=self.dir,
        )


class ElementProps(BaseModel):
    """Наши поля в props элемента: где в workspace лежит его содержимое.

    Вложения пользователя приходят без props — их всегда пишет UploadRoute в
    upload/, поэтому разбор пустых props даёт именно этот каталог.
    """

    model_config = ConfigDict(extra="ignore")

    dir: ThreadDir = ThreadDir.UPLOAD

    @classmethod
    def of(cls, raw: object) -> ElementProps:
        if not isinstance(raw, Mapping):
            return cls()

        return cls.model_validate(raw)
