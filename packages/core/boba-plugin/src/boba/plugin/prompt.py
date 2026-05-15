"""PromptOverlay: сырой overlay описаний tool'а для LLM.

Convention: Tool кладёт в свой config-DTO поле `prompt: PromptOverlay` и
в `definition()` строит свою `ObjectSchema` локально, оборачивая её в
`self._cfg.prompt.apply(...)`. Конфиг плагина может иметь несколько
prompt-полей (по одному на каждый tool в плагине) — просто как
`<name>: PromptOverlay` в pydantic-модели.

Plugin DTO:

    class ConfluencePluginConfig(BobaFlatSettings):
        confluence_search: PromptOverlay = Field(default_factory=PromptOverlay)
        confluence_page: PromptOverlay = Field(default_factory=PromptOverlay)

Tool:

    def definition(self) -> ObjectSchema[SearchArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description="...",
            fields=[FieldSpec(...), FieldSpec(...)],
            factory=SearchArgs,
        ))
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from boba.schema.declaration import (
    FieldSpec,
    ObjectSchema,
)

__all__ = ["PromptOverlay"]


T = TypeVar("T")


class PromptOverlay(BaseModel):
    """Overlay описаний tool'а: общее description и per-field overrides.

    Пустая строка в `description` или в `fields[name]` означает «оставить
    дефолт из CANONICAL». Поля, не упомянутые в `fields`, сохраняют свои
    canonical-описания.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(
        default="",
        description="Override общего описания tool'а; пусто — дефолт из кода.",
    )
    fields: dict[str, str] = Field(
        default_factory=dict,
        description="Per-field overrides: имя поля → новое описание.",
    )

    def apply(self, schema: ObjectSchema[T]) -> ObjectSchema[T]:
        """Вернуть копию `schema` с применённым overlay'ем (frozen-safe).

        Legacy-путь для tool'ов, чей TArgs ещё `@dataclass` через
        `boba.schema`. Pydantic-tool'ы зовут `apply_to_json_schema` напрямую.
        """
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

    def apply_to_json_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Вернуть копию JSON-schema dict'а с применённым overlay'ем.

        Патчит:
          * корневой `description` — если overlay.description непустой;
          * `properties[name].description` — для каждого поля из
            `overlay.fields`, у которого значение непустое.

        Пустая строка в overlay означает «оставить canonical-описание».
        Поля и ключи, не упомянутые в overlay, копируются без изменений.
        Исходный dict не мутируется (shallow + properties-копия).
        """
        result = dict(schema)
        if self.description:
            result["description"] = self.description

        props_in = schema.get("properties")
        if isinstance(props_in, dict) and self.fields:
            props_out: dict[str, Any] = {}
            for name, prop in props_in.items():
                override = self.fields.get(name) if isinstance(name, str) else None
                if override and isinstance(prop, dict):
                    new_prop = dict(prop)
                    new_prop["description"] = override
                    props_out[name] = new_prop
                else:
                    props_out[name] = prop
            result["properties"] = props_out
        return result
