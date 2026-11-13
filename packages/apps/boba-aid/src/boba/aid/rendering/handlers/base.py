"""Контракт обработчика содержимого, независимый от раскладки ленты."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar, Protocol

from chainlit.element import Element

from boba.aid.rendering.mime import Mime
from boba.canvas.canvas import CanvasKind
from boba.toolkit.result import VisualResult


@dataclass(frozen=True)
class Resource:
    """Одна из форм доставки: текст, base64 или ссылка. MIME задаёт формат."""

    mime: Mime
    name: str = ""
    text: str | None = None
    data: str | None = None
    uri: str | None = None


@dataclass(frozen=True)
class Target:
    """Место элемента задаёт ChatView; обработчик не знает роль сообщения."""

    id: str
    number: int
    emits_elements: bool
    attach: Callable[[Element, bool], Awaitable[None]]
    no_rows: str
    thumbnail: bool = False


class HandlerContext(Protocol):
    async def file(
        self, resource: Resource, target: Target, preview: CanvasKind | None
    ) -> str: ...

    def element(self, result: VisualResult) -> Element: ...


class ContentHandler(ABC):
    """render возвращает текст и добавляет элементы через Target.attach."""

    preview: ClassVar[CanvasKind | None] = None

    @abstractmethod
    async def render(
        self, resource: Resource, target: Target, context: HandlerContext
    ) -> str: ...
