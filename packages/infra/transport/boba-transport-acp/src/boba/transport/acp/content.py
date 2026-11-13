"""Стандартные блоки ACP и MCP: MIME, байты и annotations без потерь."""

from typing import ClassVar, TypeAlias

import mcp_types as mt
from pydantic import TypeAdapter, ValidationError

from acp.experimental.v2 import schema

ContentBlock: TypeAlias = (
    schema.TextContentBlock
    | schema.ImageContentBlock
    | schema.AudioContentBlock
    | schema.ResourceContentBlock
    | schema.EmbeddedResourceContentBlock
    | schema.OtherContentBlock
)


class ContentBlocks:
    """Общие блоки проходят между схемами без преобразования в текст.

    Неизвестный клиенту вид ACP получает подпись вместо молчаливой потери
    или вывода двоичного payload. Расширения boba для этого не нужны.
    """

    _acp: ClassVar[TypeAdapter[ContentBlock]] = TypeAdapter(ContentBlock)
    _mcp: ClassVar[TypeAdapter[mt.ContentBlock]] = TypeAdapter(mt.ContentBlock)

    @classmethod
    def from_mcp(cls, block: mt.ContentBlock) -> ContentBlock:
        data = block.model_dump(mode="json", by_alias=True, exclude_none=True)
        return cls._acp.validate_python(data)

    @classmethod
    def to_mcp(cls, block: ContentBlock) -> mt.ContentBlock:
        data = block.model_dump(mode="json", by_alias=True, exclude_none=True)
        try:
            return cls._mcp.validate_python(data)
        except ValidationError:
            return mt.TextContent(text=f"[Unsupported content: {block.type}]")

    @classmethod
    def tool_result(
        cls, raw: object, fallback: str
    ) -> list[schema.ContentToolCallContent]:
        """Сохранённый tools/call → стандартный content; старые и локальные
        результаты без формы MCP — текстом. Пустой content остаётся пустым.
        """
        try:
            reply = mt.CallToolResult.model_validate(raw)
        except ValidationError:
            blocks: list[ContentBlock] = [schema.TextContentBlock(text=fallback)]
        else:
            blocks = [cls.from_mcp(block) for block in reply.content]

        return [schema.ContentToolCallContent(content=block) for block in blocks]
