"""Tool: оглавление HTML-документа из workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.tools import (
    ChainConverter,
    Default,
    FieldSpec,
    IsInt,
    IsString,
    MaxValue,
    MinValue,
    NonEmpty,
    Nullable,
    ObjectSchema,
    Pass,
    Required,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)
from boba.ext.html._parse import Heading, anchor_for, collect_headings, load_soup
from boba.patterns import Converter


@dataclass(frozen=True)
class OutlineArgs:
    path: str
    max_depth: int | None
    limit: int


class OutlineArgsConverter(Converter[dict[str, Any], OutlineArgs]):
    def convert(self, value: dict[str, Any]) -> OutlineArgs:
        return OutlineArgs(
            path=value["path"],
            max_depth=value.get("max_depth"),
            limit=value["limit"],
        )


class HtmlOutlineTool(Tool[OutlineArgs]):
    """Иерархия <h1>..<h6> HTML-документа с anchor'ами для html_section."""

    _ID = ToolId("html_outline")
    _SOURCE = ToolSourceId("builtin.html")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], OutlineArgs]:
        return OutlineArgsConverter()

    def definition(self) -> ObjectSchema[dict[str, Any]]:
        return ObjectSchema(
            description=(
                "Оглавление HTML-файла: иерархия <h1>..<h6> с anchor'ами. "
                "Anchor — либо #<id> атрибута заголовка, либо #idx:N "
                "(порядковый номер). Используется как вход в html_section."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к HTML-файлу в workspace.",
                    converter=ChainConverter(Required(), IsString(), NonEmpty()),
                ),
                FieldSpec(
                    name="max_depth",
                    description=(
                        "Максимальный уровень заголовков (1=h1..6=h6). "
                        "Без значения — все 6."
                    ),
                    converter=Nullable(
                        ChainConverter(IsInt(), MinValue(1), MaxValue(6))
                    ),
                ),
                FieldSpec(
                    name="limit",
                    description="Максимум заголовков в ответе.",
                    converter=ChainConverter(Default(200), IsInt(), MinValue(1)),
                ),
            ],
            invariants=Pass(),
        )

    def execute(self, ctx: ToolContext, req: OutlineArgs) -> ToolResult:
        try:
            soup = load_soup(ctx.project_workspace, req.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Файл не найден: {req.path}"
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка чтения: {e}"
            ) from e

        headings = collect_headings(soup, max_depth=req.max_depth)
        truncated = len(headings) > req.limit
        if truncated:
            headings = headings[: req.limit]

        title = soup.title.get_text(strip=True) if soup.title else ""
        charset = soup.original_encoding or ""

        head = f"Документ: {req.path}"
        if title:
            head += f"  title={title!r}"
        if charset:
            head += f"  charset={charset}"

        if not headings:
            return ToolResult(content=f"{head}\nЗаголовков: 0")

        body = "\n".join(_render_line(h) for h in headings)
        suffix = f", truncated at limit={req.limit}" if truncated else ""
        return ToolResult(
            content=f"{head}\nЗаголовков: {len(headings)}{suffix}\n\n{body}",
        )


def _render_line(h: Heading) -> str:
    indent = "  " * (h.level - 1)
    return f"{h.index:>3}. {indent}h{h.level} {h.text}  #{anchor_for(h)}"
