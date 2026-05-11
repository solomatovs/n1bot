"""PromptOverlay: сырой overlay описаний tool'а для LLM.

Convention: Tool кладёт в свой config-DTO поле `prompt: PromptOverlay` и
в `definition()` строит свою `ObjectSchema` локально, оборачивая её в
`self._cfg.prompt.apply(...)`. Конфиг плагина может иметь несколько
prompt-полей (по одному на каждый tool в плагине) — просто как
`<name>: PromptOverlay` в dataclass; `schema_from_dataclass` собирает
`NestedField` со схемой PromptOverlay автоматически.

Plugin DTO:

    @dataclass(frozen=True)
    class ConfluencePluginConfig:
        confluence_search: PromptOverlay
        confluence_page: PromptOverlay

Tool:

    def definition(self) -> ObjectSchema[SearchArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description="...",
            fields=[FieldSpec(...), FieldSpec(...)],
            factory=SearchArgs,
        ))
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Annotated, Any, TypeVar

from boba.schema.coercion import ParseString
from boba.schema.declaration import (
    FieldSpec,
    ObjectSchema,
)

__all__ = ["PromptOverlay"]


T = TypeVar("T")


@dataclass(frozen=True)
class PromptOverlay:
    """Overlay описаний tool'а: общее description и per-field overrides.

    Пустая строка в `description` или в `fields[name]` означает «оставить
    дефолт из CANONICAL». Поля, не упомянутые в `fields`, сохраняют свои
    canonical-описания.
    """

    description: Annotated[
        str,
        "Override общего описания tool'а; пусто — дефолт из кода.",
        ParseString(),
    ] = ""
    fields: Annotated[
        dict[str, str],
        "Per-field overrides: имя поля → новое описание.",
    ] = field(default_factory=dict)

    def apply(self, schema: ObjectSchema[T]) -> ObjectSchema[T]:
        """Вернуть копию `schema` с применённым overlay'ем (frozen-safe)."""
        new_description = self.description if self.description else schema.description
        new_fields: list[Any] = []
        for f in schema.fields:
            if not isinstance(f, FieldSpec):
                new_fields.append(f)
                continue

            match self.fields.get(f.name):
                case override if override:
                    new_fields.append(replace(f, description=override))
                case _:
                    new_fields.append(replace(f, description=f.description))

        return replace(
            schema,
            description=new_description,
            fields=tuple(new_fields),
        )
