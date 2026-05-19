"""`ChromadbPluginConfig` — DTO секции `[tool.chromadb]`.

Вынесен из `plugin.py` в отдельный модуль, чтобы `di.py` мог
импортировать его без циркулярной зависимости (plugin.py → di.py →
plugin.py). И plugin.py, и di.py теперь зависят только от `config.py`.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from boba.plugin.prompt import PromptOverlay
from boba.settings import BobaFlatSettings, BobaSettingsConfigDict

__all__ = ["ChromadbPluginConfig"]


class ChromadbPluginConfig(BobaFlatSettings):
    """ChromaDB read-tools: kb_search + kb_list_collections + kb_ingest.

    `persist_path` обязателен при `enable=True`. `embedding_model='default'` —
    built-in ONNX (без сети).
    """

    model_config = BobaSettingsConfigDict(
        case_sensitive=False,
        extra="forbid",
        boba_env_prefix="BOBA_TOOL__CHROMADB__",
        boba_toml_section="tool.chromadb",
    )

    enable: bool = Field(
        default=False,
        description="Подключить плагин в discovery.",
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
            "OpenAI-совместимый endpoint embeddings. "
            "Игнорируется при model=default."
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
            "Папка с .md чанками для индексации. Использует и LLM-tool "
            "kb_ingest, и интеграционный тест `test_operator_real_ingest`. "
            "Оператор закрепляет выбор папки за собой — LLM не выбирает "
            "(защита от случайного индексирования чужих файлов). Пустая "
            "строка = ingest выключен (kb_ingest вернёт ошибку, "
            "operator-mode тест skip'ается)."
        ),
    )
    ingest_collection: str = Field(
        default="knowledge_base",
        min_length=3,
        max_length=512,
        description=(
            "Имя коллекции, в которую индексируется ingest_folder. "
            "Оператор закрепляет имя за собой, чтобы LLM не создавал "
            "коллекции на лету и не перезаписывал чужие. ChromaDB-"
            "ограничение: 3..512 символов из [a-zA-Z0-9._-], начало "
            "и конец — буквы/цифры."
        ),
    )
    ingest_collection_description: str = Field(
        default="",
        description=(
            "Description коллекции (видно в kb_list_collections). "
            "Прописывается при первом создании коллекции через "
            "`ensure_collection`."
        ),
    )
    kb_search: PromptOverlay = Field(default_factory=PromptOverlay)
    kb_list_collections: PromptOverlay = Field(default_factory=PromptOverlay)
    kb_ingest: PromptOverlay = Field(default_factory=PromptOverlay)

    @model_validator(mode="after")
    def _check_persist_path_when_enabled(self) -> Self:
        if self.enable and not self.persist_path:
            msg = "persist_path обязателен при enable=True"
            raise ValueError(msg)
        return self
