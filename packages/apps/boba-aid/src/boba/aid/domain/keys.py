"""Адресация файлов треда: ссылка на файл по его месту и её маршрут, ссылка на
канал журнала.

Ошибки: ValueError — сегмент ключа пуст, ведёт наружу каталога или называет
неизвестный каталог треда.
"""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import quote

from boba.canvas.keys import (
    ObjectKey,
)
from boba.toolkit.channels import JournalChannel

__all__ = [
    "AppPrefix",
    "CanvasFileUrl",
    "StreamUrl",
]


class AppPrefix:
    """Префикс подмонтированного приложения; ссылка без него уйдёт в корень домена.

    Значение ставит bootstrap из [chainlit].url_prefix конфига один раз на
    процесс; строители ссылок читают его отсюда, окружения не знают.
    """

    _prefix: ClassVar[str | None] = None

    @classmethod
    def install(cls, prefix: str) -> None:
        cls._prefix = prefix.rstrip("/")

    @classmethod
    def render(cls) -> str:
        if cls._prefix is None:
            msg = (
                "app prefix is not installed: bootstrap must call "
                "AppPrefix.install([chainlit].url_prefix) before links are built"
            )
            raise RuntimeError(msg)

        return cls._prefix


class CanvasFileUrl:
    """Адрес файла панели: тред, каталог и имя; пользователь — из токена.

    Содержимое панели живёт дольше сокет-сессии: оно рассылается во все
    вкладки треда и переживает переподключение. Поэтому ссылка адресует файл
    самим его местом в workspace, а не записью в памяти сессии.
    """

    ROUTE: ClassVar[str] = "/canvas/{thread_id}/{dir}/{name}"
    """Общий шаблон route и ссылки."""

    @classmethod
    def path(cls, key: ObjectKey) -> str:
        name = quote(key.name, safe="")
        route = cls.ROUTE.format(
            thread_id=key.thread_id,
            dir=key.dir.value,
            name=name,
        )

        return AppPrefix.render() + route


class StreamUrl:
    """Адрес скачивания канала журнала: тред, call_id и канал; юзер — из сессии.

    Ссылка несёт префикс подмонтированного приложения, как у файлов сессии,
    иначе GET ушёл бы в корень домена мимо роута.
    """

    ROUTE: ClassVar[str] = "/stream/{thread_id}/{call_id}"
    """Шаблон route: канал уходит в query, роут его не разбирает."""

    LINK: ClassVar[str] = ROUTE + "?channel={channel}"
    """Шаблон ссылки: тот же route плюс канал запросом."""

    @classmethod
    def path(cls, thread_id: str, call_id: str, channel: JournalChannel) -> str:
        name = quote(channel.value, safe="")
        link = cls.LINK.format(
            thread_id=thread_id,
            call_id=call_id,
            channel=name,
        )

        return AppPrefix.render() + link
