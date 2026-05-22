"""`FilesIngestConfig` — конфиг секции `[tool.kb.files]`.

Закрепляет за `files_ingest`-tool'ом FS-источник: одну папку с файлами
(`.md`/`.html`/`.htm`) и одну target-коллекцию в `kb_chunks`. LLM не
выбирает ни папку, ни коллекцию — оператор пинит их в TOML.

Разведено с Confluence-источником (`[tool.kb.confluence_ingest]`),
чтобы операторский ingest из локальных файлов и автоматический ingest
из Confluence складывались в разные коллекции и не перемешивались.
"""

from __future__ import annotations

from pydantic import Field

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["IngestFilesConfig"]


class IngestFilesConfig(BobaFlatSettings):
    """
    Конфиг для индексации документов из папки

    Config-секция: `[tool.kb.files_ingest]`.
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        config_path="tool.kb.files_ingest",
    )

    collection: str = Field(
        default="kb_files",
        min_length=1,
        max_length=255,
        description=(
            "Имя коллекции в `kb_chunks`, куда пишет `files_ingest`. "
            "Отдельная от confluence-коллекции, чтобы операторская "
            "FS-индексация не перемешивалась с автоматическим ingest'ом "
            "из Confluence."
        ),
    )
    folder: str = Field(
        default="./local/docs",
        description=(
            "Папка с файлами для `files_ingest` (`.md`/`.html`/`.htm`). "
            "LLM папку не выбирает (защита от индексирования чужих "
            "файлов) — оператор пинит её здесь."
        ),
    )
    prune: bool = Field(
        default=False,
        description=(
            "prune_missing=True: удалить из коллекции чанки, чьих "
            "source_id нет среди файлов в `[tool.kb.files].folder`."
        ),
    )
