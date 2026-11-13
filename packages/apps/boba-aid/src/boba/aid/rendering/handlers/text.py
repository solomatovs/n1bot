"""Обработчики текстовых форматов; ссылки и двоичные данные остаются файлами."""

import csv
import io
import json
from typing import ClassVar

from boba.aid.rendering.handlers.base import (
    ContentHandler,
    HandlerContext,
    Resource,
    Target,
)
from boba.canvas.canvas import CanvasElement, CanvasKind
from boba.toolkit.result import Fence, NoteLine, TableText, VisualResult


class MarkdownHandler(ContentHandler):
    preview = CanvasKind.TEXT

    async def render(
        self, resource: Resource, target: Target, context: HandlerContext
    ) -> str:
        if resource.text is None:
            return await context.file(resource, target, self.preview)

        return resource.text


class CodeHandler(ContentHandler):
    async def render(
        self, resource: Resource, target: Target, context: HandlerContext
    ) -> str:
        if resource.text is None:
            return await context.file(resource, target, self.preview)

        return Fence.around(resource.text, resource.name)


class PlainHandler(CodeHandler):
    preview = CanvasKind.STREAM


class CsvHandler(ContentHandler):
    async def render(
        self, resource: Resource, target: Target, context: HandlerContext
    ) -> str:
        if resource.text is None:
            return await context.file(resource, target, self.preview)

        rows = list(csv.DictReader(io.StringIO(resource.text)))

        if not rows:
            return NoteLine.render(target.no_rows)
        return TableText.render(rows)


class JsonHandler(ContentHandler):
    TREE_BYTES: ClassVar[int] = 1 << 20

    async def render(
        self, resource: Resource, target: Target, context: HandlerContext
    ) -> str:
        if resource.text is None:
            return await context.file(resource, target, self.preview)

        if len(resource.text.encode()) > self.TREE_BYTES:
            return await CodeHandler().render(resource, target, context)

        try:
            value = json.loads(resource.text)
        except ValueError:
            return await CodeHandler().render(resource, target, context)

        if target.emits_elements:
            tree = VisualResult(
                element=CanvasElement.JSON_TREE,
                props={"value": value},
                title=resource.name,
            )
            await target.attach(context.element(tree), True)

        return ""
