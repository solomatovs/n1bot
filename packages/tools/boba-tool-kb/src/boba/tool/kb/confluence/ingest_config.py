"""`ConfluenceIngestConfig` — конфиг секции `[tool.kb.confluence_ingest]`.

Закрепляет за confluence-ingest-tools (`confluence_space_ingest`,
`confluence_page_ingest`) target-коллекцию в `kb_chunks`. Отдельно
от `[tool.kb.confluence]`, который хранит **подключение** к Confluence
(base_url/auth) — чтобы разделить «куда ходить» и «куда складывать».

Разведено с FS-источником (`[tool.kb.files]`): автоматический ingest
из Confluence пишет в свою коллекцию и не перемешивается с тем, что
оператор индексирует руками из локальной папки.
"""

from __future__ import annotations

from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["ConfluenceIngestConfig"]


class ConfluenceIngestConfig(BobaFlatSettings):
    """Target-коллекция для confluence-ingest-tools.

    Config-секция: `[tool.kb.confluence_ingest]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.confluence_ingest",
    )

    collection: str = Field(
        default="kb_confluence",
        min_length=1,
        max_length=255,
        description=(
            "Имя коллекции в `kb_chunks`, куда пишут "
            "`confluence_space_ingest` и `confluence_page_ingest`. "
            "Отдельная от FS-коллекции, чтобы автоматический ingest "
            "из Confluence не смешивался с операторской FS-индексацией."
        ),
    )
