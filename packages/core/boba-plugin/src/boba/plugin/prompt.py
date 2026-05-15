"""PromptOverlay: сырой overlay описаний tool'а для LLM.

Convention: Tool кладёт в свой config-DTO поле `prompt: PromptOverlay`.
Tool ABC вызывает `prompt.apply_to_json_schema(...)` поверх
`model_json_schema()` Args-модели — патчит description'ы для LLM, не
трогая саму pydantic-модель валидации.

Конфиг плагина может иметь несколько prompt-полей (по одному на каждый
tool в плагине) — просто как `<name>: PromptOverlay` в pydantic-модели.

Plugin DTO:

    class ConfluencePluginConfig(BobaFlatSettings):
        confluence_search: PromptOverlay = Field(default_factory=PromptOverlay)
        confluence_page: PromptOverlay = Field(default_factory=PromptOverlay)

Tool config DTO:

    class ConfluenceSearchToolConfig:
        base_url: str
        ...
        prompt: PromptOverlay
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["PromptOverlay"]


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
