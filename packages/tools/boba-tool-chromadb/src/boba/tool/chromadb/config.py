"""`ChromadbPluginConfig` — конфиг секции `[tool.chromadb]` (v2)."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from boba.settings import BobaFlatSettings, BobaSettingsConfigDict, StringList

__all__ = ["ChromadbPluginConfig"]


class ChromadbPluginConfig(BobaFlatSettings):
    """ChromaDB tools: kb_search + kb_list_collections + kb_ingest.

    `persist_path` обязателен при `enable=True`. `embedding_model='default'` —
    built-in ONNX (offline). Используется и tool'ами (через FromConfig),
    и DI-provider'ами (Client/KB/Indexer).
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        config_path="tool.chromadb",
    )

    enable: bool = Field(
        default=False,
        description="Регистрировать ли chromadb-tools в DI/каталоге LLM.",
    )
    tools: StringList | None = Field(
        default=None,
        description=(
            "Allowlist tool-имён: None — все включены; иначе только "
            "перечисленные ('kb_search', 'kb_list_collections', 'kb_ingest')."
        ),
    )
    persist_path: str = Field(
        default="",
        description="Путь к persistent ChromaDB (обязателен при enable=True).",
    )
    embedding_model: str = Field(
        default="default",
        description=(
            "'default' = built-in ONNX all-MiniLM-L6-v2; "
            "иначе — модель LiteLLM/OpenAI-API."
        ),
    )
    embedding_base_url: str = Field(
        default="",
        description=(
            "OpenAI-совместимый endpoint embeddings. Игнорируется при model=default."
        ),
    )
    embedding_api_key: str = Field(
        default="",
        description="API key embeddings endpoint'а.",
    )
    snippet_chars: int = Field(
        default=300,
        ge=1,
        description="Максимальная длина сниппета документа в kb_search.",
    )
    max_top_k: int = Field(
        default=20,
        ge=1,
        description="Жёсткий потолок параметра top_k.",
    )
    ingest_folder: str = Field(
        default="",
        description=(
            "Папка с .md чанками для индексации. Оператор закрепляет выбор "
            "папки за собой — LLM не выбирает (защита от случайного "
            "индексирования чужих файлов). Пустая строка = ingest выключен."
        ),
    )
    ingest_collection: str = Field(
        default="knowledge_base",
        min_length=3,
        max_length=512,
        description=(
            "Имя коллекции, в которую индексируется ingest_folder. "
            "ChromaDB-ограничение: 3..512 символов."
        ),
    )
    ingest_collection_description: str = Field(
        default="",
        description=(
            "Description коллекции (видно в kb_list_collections). "
            "Прописывается при первом создании коллекции."
        ),
    )

    @model_validator(mode="after")
    def _check_persist_path_when_enabled(self) -> Self:
        if self.enable and not self.persist_path:
            msg = "persist_path обязателен при enable=True"
            raise ValueError(msg)
        return self
