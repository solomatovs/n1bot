"""Адресация вложений: storage-ключ, путь в песочнице, ссылка и её маршрут.

Ошибки: ValueError — сегмент ключа пуст, ведёт наружу каталога или называет
неизвестный каталог треда.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from boba.sandbox import WORKSPACE_MOUNT
from boba.toolkit.channels import StreamKey

__all__ = [
    "AttachmentLinks",
    "AttachmentUrl",
    "DirKey",
    "ElementProps",
    "KeyField",
    "ObjectKey",
    "StreamUrl",
    "ThreadDir",
]


class ThreadDir(StrEnum):
    """Каталоги треда: вложения пользователя и спеки диаграмм."""

    UPLOAD = "upload"
    MERMAID = "mermaid"


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
            raise ValueError(f"invalid key segment: {value!r}")

        if cls.SEPARATOR in value:
            raise ValueError(f"invalid key segment: {value!r}")

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
            raise ValueError(f"invalid object_key: {raw!r}")

        order = (KeyField.USER_ID, KeyField.THREAD_ID, KeyField.DIR, KeyField.NAME)
        fields = dict(zip(order, parts, strict=True))

        try:
            return cls.model_validate(fields)
        except ValidationError as e:
            raise ValueError(f"invalid object_key: {raw!r}") from e

    @classmethod
    def from_workspace(cls, user_id: object, thread_id: object, path: str) -> Self:
        """Ключ по пути из песочницы; вне каталогов треда — ValueError."""
        rel = path.strip()

        mount = cls.SEPARATOR.join((WORKSPACE_MOUNT, ""))
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
            (WORKSPACE_MOUNT, str(thread_id), f"{{{dirs}}}", name)
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
        return self.SEPARATOR.join((WORKSPACE_MOUNT, self.in_thread()))

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
            raise ValueError(f"invalid dir key: {raw!r}")

        order = (KeyField.USER_ID, KeyField.THREAD_ID, KeyField.DIR)
        fields = dict(zip(order, parts, strict=True))

        try:
            return cls.model_validate(fields)
        except ValidationError as e:
            raise ValueError(f"invalid dir key: {raw!r}") from e

    def render(self) -> str:
        return self.SEPARATOR.join((self.user_id, self.in_thread()))

    def in_thread(self) -> str:
        """Путь каталога внутри образа пользователя."""
        return self.SEPARATOR.join((self.thread_id, self.dir))

    def in_workspace(self) -> str:
        """Путь каталога так, как его видит песочница."""
        return self.SEPARATOR.join((WORKSPACE_MOUNT, self.in_thread()))

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


@dataclass(frozen=True, slots=True)
class AttachmentUrl:
    """Адресация вложения по треду, каталогу и id элемента — не по пути в хранилище.

    Каталог в ссылке обязателен: без него отдача искала бы файл только в
    upload/, и вложение из mermaid/ отвечало бы 404.
    """

    thread_id: str
    dir: ThreadDir
    element_id: str

    ROUTE: ClassVar[str] = "/attachment/{thread_id}/{dir}/{element_id}"
    """Общий шаблон route и ссылки."""

    def path(self) -> str:
        return self.ROUTE.format(
            thread_id=self.thread_id,
            dir=self.dir.value,
            element_id=self.element_id,
        )


@dataclass(frozen=True, slots=True)
class AttachmentLinks:
    """Выдаёт публичные ссылки на вложения; url-префикс знает только он."""

    prefix: str

    def url(self, thread_id: object, element_id: object, dir_thread: ThreadDir) -> str:
        path = AttachmentUrl(str(thread_id), dir_thread, str(element_id)).path()
        return self.prefix.rstrip("/") + path


class StreamUrl:
    """Адрес скачивания журнала канала: тред, вызов, стадия и канал.

    Пользователь в адрес не входит — его даёт сессия, поэтому чужой том по
    ссылке недостижим. Ссылка несёт префикс подмонтированного приложения, как
    у файлов сессии, иначе GET ушёл бы в корень домена мимо роута.
    """

    ROUTE: ClassVar[str] = "/stream/{thread_id}/{call_id}/{stage}/{channel}"
    ROOT_PATH_ENV: ClassVar[str] = "CHAINLIT_ROOT_PATH"

    @classmethod
    def path(cls, key: StreamKey) -> str:
        prefix = os.getenv(cls.ROOT_PATH_ENV, "").rstrip("/")
        route = cls.ROUTE.format(
            thread_id=key.thread_id,
            call_id=key.call_id,
            stage=key.stage,
            channel=key.channel.value,
        )

        return f"{prefix}{route}"
