"""`ConfluenceIngestConfig` — конфиг секции `[tool.kb.confluence_ingest]`.

Operator-mode настройка confluence-источника + целевой коллекции для
`kb_ingest_confluence`-тула. Отделён от `ConfluencePluginConfig`
(`[tool.kb.confluence]`, connection only) и от `KbPluginConfig`
(`[tool.kb]`, DB/embeddings/FS-ingest) намеренно — три независимых
конфига для трёх независимых ингестов:

- `KbPluginConfig.ingest_*`       → FS-папка → kb_chunks
- `ConfluencePluginConfig`              → Confluence connection
                                          (общий для read-side tools)
- `ConfluenceIngestConfig` (этот файл)  → Confluence-источник → kb_chunks

`source_type` выбирает между тремя `RequestSource`'ами:
- `space` → `ConfluenceSpaceRequestSource(space_key=…)`
- `cql`   → `ConfluenceCqlRequestSource(cql=…)`
- `pages` → `ConfluencePagesRequestSource(page_ids=…)`

Cross-field validator проверяет, что соответствующее поле заполнено.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["ConfluenceIngestConfig"]


class ConfluenceIngestConfig(BobaFlatSettings):
    """Operator-mode params для `kb_ingest_confluence`."""

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence_ingest",
    )

    source_type: Literal["space", "cql", "pages"] = Field(
        default="space",
        description=(
            "Тип источника confluence-страниц. `space` — все страницы space "
            "(требует `space_key`); `cql` — CQL-запрос (требует `cql`); "
            "`pages` — явный список page-id'ов (требует `page_ids`)."
        ),
    )
    space_key: str = Field(
        default="",
        description=(
            "Confluence space key (например, `DOCS`). Обязателен при "
            "`source_type='space'`, иначе игнорируется."
        ),
    )
    cql: str = Field(
        default="",
        description=(
            "CQL-запрос (например, `space = DOCS AND lastModified > '2024-01-01'`). "
            "Обязателен при `source_type='cql'`, иначе игнорируется."
        ),
    )
    page_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Явный список page-id'ов для индексации. Обязателен при "
            "`source_type='pages'`, иначе игнорируется."
        ),
    )
    collection: str = Field(
        default="confluence_kb",
        min_length=1,
        max_length=255,
        description=(
            "Имя коллекции (значение колонки `collection` в `kb_chunks`), "
            "в которую индексируется Confluence-источник."
        ),
    )
    collection_description: str = Field(
        default="",
        description=(
            "Description коллекции (видно в kb_list_collections). "
            "Прописывается при первом ensure_collection."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        """Cross-field: source_type выбирает обязательное поле.

        Под discover-flow конфиг загружается только если в allowlist
        присутствует `kb_ingest_confluence`. Значит при load-time
        соответствующее source-поле должно быть валидно заполнено.
        """
        match self.source_type:
            case "space" if not self.space_key:
                msg = (
                    "kb.confluence_ingest.space_key обязателен "
                    "при source_type='space'"
                )
                raise ValueError(msg)
            case "cql" if not self.cql:
                msg = (
                    "kb.confluence_ingest.cql обязателен "
                    "при source_type='cql'"
                )
                raise ValueError(msg)
            case "pages" if not self.page_ids:
                msg = (
                    "kb.confluence_ingest.page_ids обязателен "
                    "при source_type='pages'"
                )
                raise ValueError(msg)
        return self
