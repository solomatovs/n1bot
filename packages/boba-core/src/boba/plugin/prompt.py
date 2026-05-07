"""PromptOverlay: сырой overlay описаний tool'а для LLM.

Convention: Tool кладёт в свой config-DTO поле `prompt: PromptOverlay` и
в `definition()` строит свою `ObjectSchema` локально, оборачивая её в
`self._cfg.prompt.apply(...)`. Конфиг плагина может иметь несколько
prompt-полей (по одному на каждый tool в плагине) — каждое объявляется
через `prompt_field(name)`.

Schema плагина:

    fields=[
        *ConfluenceConnection.fields(),
        prompt_field("confluence_search"),
        prompt_field("confluence_page"),
    ]

Tool:

    def definition(self) -> ObjectSchema[SearchArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description="...",
            fields=[FieldSpec(...), FieldSpec(...)],
            factory=SearchArgs,
        ))
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from boba.coercion import (
    ChainCoercer,
    Default,
    ParseString,
)
from boba.declaration import (
    CollectionField,
    FieldSpec,
    KeyedShape,
    NestedField,
    ObjectSchema,
    ScalarItem,
)

__all__ = [
    "PROMPT_OVERLAY_SCHEMA",
    "PromptOverlay",
    "prompt_field",
]


T = TypeVar("T")


@dataclass(frozen=True)
class PromptOverlay:
    """Overlay описаний: общее description tool'а и per-field overrides.

    Пустая строка в `description` или в `fields[name]` означает «оставить
    дефолт из CANONICAL». Поля, не упомянутые в `fields`, сохраняют свои
    canonical-описания.
    """

    description: str = ""
    fields: Mapping[str, str] = field(default_factory=dict)

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


PROMPT_OVERLAY_SCHEMA: ObjectSchema[PromptOverlay] = ObjectSchema(
    description="Overlay описаний tool'а: общее description и per-field overrides.",
    fields=[
        FieldSpec(
            name="description",
            coercer=ChainCoercer(Default(""), ParseString()),
            description="Override общего описания tool'а; пусто — дефолт из кода.",
        ),
        CollectionField(
            name="fields",
            reader=ScalarItem(coercer=ChainCoercer(Default(""), ParseString())),
            shape=KeyedShape(),
            description="Per-field overrides: имя поля → новое описание.",
        ),
    ],
    factory=PromptOverlay,
)


def prompt_field(name: str) -> NestedField[PromptOverlay]:
    """Convenience: NestedField с PROMPT_OVERLAY_SCHEMA под указанным именем.

    Использование в Plugin.config():

        fields=[
            *ConfluenceConnection.fields(),
            prompt_field("confluence_search"),
            prompt_field("confluence_page"),
        ]
    """
    return NestedField(name=name, schema=PROMPT_OVERLAY_SCHEMA)
