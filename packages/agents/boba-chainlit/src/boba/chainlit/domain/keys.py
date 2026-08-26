"""Адресация вложений: storage-ключ, путь в песочнице, ссылка и её маршрут.

Ошибки: ValueError — сегмент ключа пуст, ведёт наружу каталога или называет
неизвестный каталог треда.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import quote

from boba.canvas.keys import (
    ObjectKey,
    ThreadDir,
)
from boba.toolkit.channels import JournalChannel

__all__ = [
    "AttachmentLinks",
    "AttachmentUrl",
    "CanvasFileUrl",
    "StreamUrl",
]


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


class CanvasFileUrl:
    """Адрес файла панели: тред, каталог и имя; пользователь — из токена.

    Содержимое панели живёт дольше сокет-сессии: оно рассылается во все
    вкладки треда и переживает переподключение. Поэтому ссылка адресует файл
    самим его местом в workspace, а не записью в памяти сессии.
    """

    ROUTE: ClassVar[str] = "/canvas/{thread_id}/{dir}/{name}"
    ROOT_PATH_ENV: ClassVar[str] = "CHAINLIT_ROOT_PATH"

    @classmethod
    def path(cls, key: ObjectKey) -> str:
        prefix = os.getenv(cls.ROOT_PATH_ENV, "").rstrip("/")
        name = quote(key.name, safe="")
        return f"{prefix}/canvas/{key.thread_id}/{key.dir.value}/{name}"


class StreamUrl:
    """Адрес скачивания канала журнала: тред, call_id и канал; юзер — из сессии.

    Ссылка несёт префикс подмонтированного приложения, как у файлов сессии,
    иначе GET ушёл бы в корень домена мимо роута.
    """

    ROUTE: ClassVar[str] = "/stream/{thread_id}/{call_id}"
    ROOT_PATH_ENV: ClassVar[str] = "CHAINLIT_ROOT_PATH"

    @classmethod
    def path(cls, thread_id: str, call_id: str, channel: JournalChannel) -> str:
        prefix = os.getenv(cls.ROOT_PATH_ENV, "").rstrip("/")
        name = quote(channel.value, safe="")
        return f"{prefix}/stream/{thread_id}/{call_id}?channel={name}"
