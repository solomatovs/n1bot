"""Tool: оглавление Confluence-export'а с scroll-bookmark-anchor'ами."""

from __future__ import annotations

from dataclasses import dataclass

from boba_next.declaration import FieldSpec, ObjectSchema
from boba_next.tools import (
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
)
from boba_next.validators import (
    ChainConverter,
    Default,
    IsInt,
    IsString,
    MaxValue,
    MinValue,
    NonEmpty,
    Nullable,
)
from boba_next.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)

from boba.ext.confluence._parse import (
    Heading,
    anchor_for,
    collect_headings,
    load_soup,
)


@dataclass(frozen=True)
class OutlineArgs:
    path: str
    max_depth: int | None
    limit: int


class ConfluenceOutlineTool(Tool[OutlineArgs]):
    """Иерархия <h1>..<h6> Confluence-export'а с anchor'ами для confluence_section."""

    _ID = ToolId("confluence_outline")
    _SOURCE = ToolSourceId("builtin.confluence")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[OutlineArgs]:
        return ObjectSchema(
            description=(
                "Оглавление Confluence-export'а: иерархия <h1>..<h6> с "
                "scroll-bookmark-anchor'ами, извлечёнными из <ac:structured-macro "
                "ac:name='anchor'>. Anchor подаётся в confluence_section как есть."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к HTML-файлу Confluence-export'а в workspace.",
                    converter=ChainConverter(IsString(), NonEmpty()),
                    required=True,
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
            factory=OutlineArgs,
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
