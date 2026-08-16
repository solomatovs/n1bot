"""Канвас: единая панель справа от чата и реестр вьюверов файлов workspace.

Панель показывает содержимое одного файла — того, что открыла LLM тулом или
ткнул пользователь по ссылке. Какой элемент рисует содержимое и следить ли
за файлом, решает вьювер, зарегистрированный на расширение. Вердикт рендера
браузер возвращает действием canvas_render_status: вьювер ждёт его по nonce.

Ошибки: CanvasError — файл некому показать или он не отрисовался.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

import chainlit as cl
from boba.chainlit.domain.errors import RefusalError
from boba.chainlit.domain.keys import ObjectKey
from boba.toolkit.result import CustomElementResult, DiagramResult

__all__ = [
    "CanvasAction",
    "CanvasContent",
    "CanvasError",
    "CanvasErrorKind",
    "CanvasKind",
    "CanvasPanel",
    "CanvasPush",
    "CanvasRegistry",
    "CanvasViewer",
    "OpenedCanvas",
    "RenderStatus",
    "RenderVerdict",
    "RenderVerdicts",
    "StreamPos",
]

logger = logging.getLogger(__name__)


class CanvasErrorKind(StrEnum):
    """Коды отказов панели: уезжают в ErrorResult.error_kind."""

    BAD_PATH = "bad_path"
    NO_VIEWER = "no_canvas_viewer"
    FILE_NOT_FOUND = "file_not_found"
    BAD_FILE = "bad_file"
    TOO_LARGE = "file_too_large"
    RENDER_FAILED = "render_failed"


class CanvasError(RefusalError):
    """Панель не показала файл; kind — код причины, текст готов для LLM.

    kind шире собственного enum панели: вьювер поднимает сюда свои коды
    (file_not_found и подобные) как есть.
    """


class CanvasAction(StrEnum):
    """Действия фронта для канваса и поля их payload."""

    OPEN = "canvas_open"
    PATH = "path"
    STATUS = "canvas_render_status"
    CONTENT = "canvas_content"
    NONCE = "nonce"


class RenderStatus(StrEnum):
    """Исход рендера в браузере."""

    RENDERED = "rendered"
    FAILED = "failed"
    UNKNOWN = "unknown"
    """Браузер не ответил за отведённое время (вкладка закрыта)."""


class RenderVerdict(BaseModel):
    """Вердикт рендера одного показа: статус и текст ошибки mermaid."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: RenderStatus
    message: str = ""


class RenderReport(BaseModel):
    """Payload действия canvas_render_status: разбирается на границе."""

    model_config = ConfigDict(extra="ignore")

    nonce: str
    ok: bool
    error: str = ""

    def verdict(self) -> RenderVerdict:
        if self.ok:
            return RenderVerdict(status=RenderStatus.RENDERED)

        return RenderVerdict(status=RenderStatus.FAILED, message=self.error)


class RenderVerdicts:
    """Ожидания вердиктов рендера: nonce показа -> future с исходом.

    Валидатор синтаксиса один — mermaid.js в браузере: сервер не гадает,
    а ждёт его ответ. Отчёт на незнакомый nonce (после таймаута или от
    повторного пуша вотчера) молча пропускается — ждать его уже некому.
    """

    _WAITERS: ClassVar[dict[str, asyncio.Future[RenderVerdict]]] = {}

    @classmethod
    def expect(cls, nonce: str) -> None:
        """Начать ждать вердикт; зовётся до показа элемента, чтобы не было гонки."""
        loop = asyncio.get_running_loop()
        cls._WAITERS[nonce] = loop.create_future()

    @classmethod
    def report(cls, payload: Mapping[str, object]) -> None:
        """Принять отчёт браузера; битый payload — ValidationError наверх.

        Ожидание остаётся в реестре: браузер может ответить раньше, чем
        вьювер дойдёт до wait, — забирает и чистит запись только wait.
        """
        parsed = RenderReport.model_validate(payload)

        waiter = cls._WAITERS.get(parsed.nonce)
        if waiter is None:
            logger.debug(
                "render verdict dropped: nobody waits for nonce %s (ok=%s)",
                parsed.nonce,
                parsed.ok,
            )
            return

        if waiter.done():
            return

        waiter.set_result(parsed.verdict())

    @classmethod
    async def wait(cls, nonce: str, timeout_sec: float) -> RenderVerdict:
        """Дождаться вердикта; молчание браузера — UNKNOWN, не ошибка."""
        waiter = cls._WAITERS.get(nonce)
        if waiter is None:
            return RenderVerdict(status=RenderStatus.UNKNOWN)

        try:
            return await asyncio.wait_for(waiter, timeout_sec)
        except TimeoutError:
            return RenderVerdict(status=RenderStatus.UNKNOWN)
        finally:
            cls._WAITERS.pop(nonce, None)


class CanvasKind(StrEnum):
    """Чем рисовать содержимое в панели: выбирает вьювер, рисует CanvasView."""

    MERMAID = "mermaid"
    IMAGE = "image"
    PDF = "pdf"
    TEXT = "text"
    VIDEO = "video"
    AUDIO = "audio"
    STREAM = "stream"
    """Живой вывод инструмента: хвост окна, панель мотает вниз сама."""
    NOTICE = "notice"
    """Формат показать некому: панель объясняет это вместо содержимого."""


class StreamPos(BaseModel):
    """Координаты окна потока в журнале: где стоим и сколько всего."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    offset: int
    end: int
    """Байт за последним в окне: сюда фронт стыкует следующее окно."""
    size: int
    window: int
    closed: bool
    follow: bool
    """Показ хвоста по запросу или насосом: фронт встаёт на низ окна."""


class CanvasContent(BaseModel):
    """Описание содержимого панели: что рисовать и откуда взять.

    Сервер описывает, фронт рисует — поэтому смена файла в открытой панели
    не требует нового элемента и не переоткрывает её.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CanvasKind
    path: str
    label: str
    url: str = ""
    """Ссылка на файл в storage: картинки, pdf, видео, аудио."""
    text: str = ""
    """Содержимое как текст: спека mermaid, markdown, лог."""
    mime: str = ""
    note: str = ""
    """Причина для notice: почему файл не показан."""
    nonce: str = ""
    """Метка показа: по ней ждут вердикт рендера mermaid."""
    stream: StreamPos | None = None
    """Окно потока: есть только у kind = stream."""

    def props(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


@dataclass(frozen=True, slots=True)
class OpenedCanvas:
    """Открытый в панели файл: подпись, путь и его представление в ленте.

    link — результат, который остаётся в переписке и по клику открывает панель:
    кнопка-ссылка для обычных файлов, отрисованная диаграмма для .mmd.
    """

    label: str
    path: str
    link: CustomElementResult | DiagramResult


class CanvasPush(Protocol):
    """Показ содержимого в панели; повторный вызов заменяет его."""

    @abstractmethod
    async def __call__(self, content: CanvasContent) -> None: ...


class CanvasViewer(Protocol):
    """Файлы одного вида: какие расширения умеет и чем их описать."""

    suffixes: ClassVar[frozenset[str]]
    """Расширения вьювера: панель перечисляет их, когда файл показать некому."""

    @abstractmethod
    def handles(self, name: str) -> bool:
        """Берётся ли вьювер за файл с таким именем."""
        ...

    @abstractmethod
    async def open(self, key: ObjectKey, push: CanvasPush) -> OpenedCanvas:
        """Показать файл через push; вернуть подпись, путь и элемент для ленты."""
        ...

    @abstractmethod
    async def content(self, key: ObjectKey) -> CanvasContent:
        """Описание файла для панели; панель его только доставляет."""
        ...


class CanvasRegistry:
    """Вьюверы канваса: файл -> кто его рисует; порядок регистрации решает спор.

    Реестр общий на приложение: панель открывается и вне сборки тулов — из
    действия пользователя. Тулы собираются на каждую сессию, поэтому
    регистрация идемпотентна по типу вьювера.
    """

    _VIEWERS: ClassVar[list[CanvasViewer]] = []

    @classmethod
    def register(cls, viewer: CanvasViewer) -> None:
        kind = type(viewer)

        for index, known in enumerate(cls._VIEWERS):
            if type(known) is not kind:
                continue
            cls._VIEWERS[index] = viewer
            return

        cls._VIEWERS.append(viewer)

    @classmethod
    def reset(cls) -> None:
        """Сброс реестра: пользуются тесты, приложению это не нужно."""
        cls._VIEWERS.clear()

    @classmethod
    def viewer_for(cls, name: str) -> CanvasViewer | None:
        for viewer in cls._VIEWERS:
            if viewer.handles(name):
                return viewer
        return None

    @classmethod
    def viewers_hint(cls) -> str:
        """Какие вьюверы зарегистрированы — для логов и диагностики."""
        if not cls._VIEWERS:
            return "none registered"

        names: list[str] = []
        for viewer in cls._VIEWERS:
            names.append(type(viewer).__name__)

        return ", ".join(sorted(names))

    @classmethod
    def suffixes_hint(cls) -> str:
        """Какие расширения панель показывает — для пользователя и LLM."""
        known: set[str] = set()
        for viewer in cls._VIEWERS:
            known.update(viewer.suffixes)

        if not known:
            return "no viewers registered"

        return ", ".join(sorted(known))


class CanvasPanel:
    """Панель справа от чата: содержимое одного файла, выбранного из чата.

    Каждый показ уходит под новым key: панель держит свой снапшот элементов
    и с прежним key его не перечитывает.
    """

    TITLE: ClassVar[str] = "canvas"
    """Магическое имя: фронт chainlit включает по нему canvas-режим панели."""

    CONTENT_ID: ClassVar[str] = "boba-canvas-content"
    """Один id на слот содержимого: React обновляет элемент, а не пересоздаёт —
    панель не мигает при смене файла, а вьюверы не теряют своё состояние."""

    VIEW_ELEMENT: ClassVar[str] = "CanvasView"
    """Единственный компонент панели: рисует любое содержимое по его kind.

    Один компонент на все типы позволяет менять файл, не подменяя элемент:
    фронт сам забирает следующее содержимое, и панель не открывается заново.
    """

    @classmethod
    async def open(cls, key: ObjectKey) -> OpenedCanvas:
        viewer = CanvasRegistry.viewer_for(key.name)
        if viewer is None:
            await cls._explain(key)
            hint = CanvasRegistry.suffixes_hint()
            msg = f"no canvas viewer for {key.in_workspace()}; supported: {hint}"
            raise CanvasError(CanvasErrorKind.NO_VIEWER, msg)

        return await viewer.open(key, cls._push)

    @classmethod
    async def show(cls, content: CanvasContent) -> None:
        """Показ готового содержимого без вьювера: живой поток инструмента."""
        await cls._push(content)

    @classmethod
    def notice(cls, key: ObjectKey) -> CanvasContent:
        """Объяснение вместо содержимого: файл показать некому."""
        return CanvasContent(
            kind=CanvasKind.NOTICE,
            path=key.in_workspace(),
            label=key.name,
            note=(
                "The panel cannot display this file format. Supported: "
                f"{CanvasRegistry.suffixes_hint()}"
            ),
        )

    @classmethod
    async def _explain(cls, key: ObjectKey) -> None:
        """Файл некому показать: в панели остаётся объяснение, а не прошлый файл."""
        await cls._push(cls.notice(key))

    @classmethod
    async def _push(cls, content: CanvasContent) -> None:
        """Открывает панель message-элементом с display='side'.

        chainlit сам держит side view по таким элементам: пока элемент цел,
        MessagesContainer не закрывает панель на каждый новый ход (в отличие от
        ElementSidebar, который он сбрасывает, когда среди элементов сообщения
        нет ни одного 'side'). Заголовок 'canvas' выставляется отдельно —
        MessagesContainer взял бы под него имя компонента, а по 'canvas' фронт
        включает полноэкранный canvas-режим панели.
        """
        element = cl.CustomElement(
            name=cls.VIEW_ELEMENT, props=content.props(), display="side"
        )
        element.id = cls.CONTENT_ID
        await element.send(for_id="")
        await cl.ElementSidebar.set_title(cls.TITLE)
