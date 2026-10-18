"""Адресация файлов треда: ссылка на файл по его месту и её маршрут, ссылка на
канал журнала.

Ошибки: ValueError — сегмент ключа пуст, ведёт наружу каталога, называет
неизвестный каталог треда, путь ссылки не маршрут файла панели либо ссылка
называет файл другого треда.
"""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import quote, unquote, urlencode

from boba.canvas.canvas import CanvasScope
from boba.canvas.keys import ObjectKey, ThreadDir, WorkspaceKey
from boba.toolkit.channels import JournalChannel

__all__ = [
    "AppPrefix",
    "CanvasFileUrl",
    "StreamUrl",
]


class AppPrefix:
    """Префикс подмонтированного приложения; ссылка без него уйдёт в корень домена.

    Значение ставит bootstrap из [aid].url_prefix конфига один раз на
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
                "AppPrefix.install([aid].url_prefix) before links are built"
            )
            raise RuntimeError(msg)

        return cls._prefix


class CanvasFileUrl:
    """Адрес файла панели: тред, каталог и имя; пользователь — из токена.

    Вид файла едет запросом (MIME): сервер мог назвать его не так, как
    подсказывает суффикс имени. Содержимое панели живёт дольше сокет-сессии:
    оно рассылается во все
    вкладки треда и переживает переподключение. Поэтому ссылка адресует файл
    самим его местом в workspace, а не записью в памяти сессии.
    """

    ROUTE: ClassVar[str] = "/canvas/{thread_id}/{dir}/{name}"
    """Общий шаблон route и ссылки."""

    MIME: ClassVar[str] = "mime"
    """Параметр запроса ссылки: вид файла, каким его назвал сервер; по нему
    маршрут отдаёт Content-Type и решает, показать файл или скачать."""

    @classmethod
    def path(cls, key: ObjectKey, mime: str) -> str:
        name = quote(key.name, safe="")
        route = cls.ROUTE.format(
            thread_id=key.thread_id,
            dir=key.dir.value,
            name=name,
        )
        query = urlencode({cls.MIME: mime})

        return f"{AppPrefix.render()}{route}?{query}"

    @classmethod
    def key(cls, scope: CanvasScope, path: str) -> WorkspaceKey:
        """Ключ файла треда области scope по пути ссылки (без хоста и
        запроса): хвост пути — маршрут с тредом, каталогом и именем.

        Ошибки:
        ValueError — путь не маршрут файла панели, файл другого треда либо
            ключ не проходит проверку сегментов.
        """
        route = cls.ROUTE.split("/")
        expected = f"canvas file link {path!r}: expected a path ending with {cls.ROUTE}"
        tail = path.split("/")[-len(route) :]
        if len(tail) != len(route):
            raise ValueError(expected)

        if tail[1] != route[1]:
            raise ValueError(expected)

        if tail[2] != scope.thread_id:
            msg = (
                f"canvas file link {path!r}: expected a file of thread "
                f"{scope.thread_id}, got thread {tail[2]}"
            )
            raise ValueError(msg)

        return WorkspaceKey(
            user_id=scope.user_id,
            thread_id=tail[2],
            dir=ThreadDir(tail[3]),
            name=unquote(tail[4]),
            workspace=scope.workspace,
        )


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
