"""Доменные модели — чистый Python, без зависимостей от UI-фреймворков."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# Конфигурация приложения
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AppConfig:
    """Единственный источник конфигурации приложения.

    Все секреты и переменные окружения читаются здесь.
    Остальные модули получают значения через cfg.*.
    """
    chroma_db_path: str = field(default_factory=lambda: Path(AppConfig._secret("CHROMA_DB_PATH")).as_posix())
    litellm_url: str = field(default_factory=lambda: AppConfig._secret("LITELLM_URL"))
    litellm_api_key: str = field(default_factory=lambda: AppConfig._secret("LITELLM_API_KEY"))
    confluence_url: str = field(default_factory=lambda: AppConfig._secret("CONFLUENCE_URL"))
    confluence_token: str = field(default_factory=lambda: AppConfig._secret("CONFLUENCE_TOKEN"))
    default_collection: str = field(default_factory=lambda: AppConfig._secret("DEFAULT_COLLECTION"))
    default_model: str = field(default_factory=lambda: AppConfig._secret("LLM_MODEL"))
    embedding_model: str = field(default_factory=lambda: AppConfig._secret("EMBEDDING_MODEL"))
    llm_timeout: int = field(default_factory=lambda: int(AppConfig._secret("LLM_TIMEOUT", "120")))
    embedding_timeout: int = field(default_factory=lambda: int(AppConfig._secret("EMBEDDING_TIMEOUT", "120")))
    log_level: str = field(default_factory=lambda: AppConfig._secret("LOG_LEVEL", "INFO"))

    @staticmethod
    def _secret(key: str, default: str = "") -> str:
        """Получить значение из переменной окружения."""
        return os.environ.get(key, default)

    @property
    def litellm_base_url(self) -> str:
        """Базовый URL без /v1 — для эмбеддингов и прямых запросов."""
        return self.litellm_url.rstrip("/").removesuffix("/v1")

    @property
    def openai_url(self) -> str:
        """URL с /v1 — для OpenAI-совместимого API."""
        return f"{self.litellm_base_url}/v1"


# ---------------------------------------------------------------------------
# Типы контента
# ---------------------------------------------------------------------------

class ContentType(Enum):
    """Типы контента в векторном хранилище."""
    TEXT = "text", "Текст"
    CODE = "code", "Код"
    TABLE = "table", "Таблицы"
    PARAGRAPH = "paragraph", "Параграфы"
    LIST = "list", "Списки"

    def __init__(self, key: str, label: str) -> None:
        self._key = key
        self._label = label

    @property
    def key(self) -> str:
        return self._key

    @property
    def label(self) -> str:
        return self._label

    @classmethod
    def labels_to_keys(cls, labels: List[str]) -> list[str] | None:
        """Преобразовать выбранные labels в ключи. Пустой список → None."""
        label_map = {ct.label: ct.key for ct in cls}
        keys = [label_map[lb] for lb in labels if lb in label_map]
        return keys or None


# ---------------------------------------------------------------------------
# Параметры поиска и генерации
# ---------------------------------------------------------------------------

@dataclass
class SearchParams:
    """Параметры поиска и генерации, управляемые пользователем."""
    # -- поиск --
    top_n: int = 12
    answers_per_variant: int = 3
    per_page: int = 1
    content_types: list[str] | None = None
    # -- multi-query --
    use_multi_query: bool = True
    mq_variants: int = 3
    k_per_variant: int = 6
    mq_prompt_template: str = "Дай {n} кратких переформулировок запроса; по одной на строку.\nЗапрос: {query}"
    # -- генерация --
    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: Optional[int] = None
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0

    def llm_kwargs(self) -> dict:
        """Параметры генерации для передачи в OpenAI API."""
        kwargs: dict = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
        }
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        return kwargs


# ---------------------------------------------------------------------------
# Параметры чанкинга
# ---------------------------------------------------------------------------

@dataclass
class ChunkingParams:
    """Параметры чанкинга документов."""
    max_tokens: int = 500
    similarity_threshold: float = 0.7
    embedding_timeout: int = 120
    code_ratio_threshold: float = 0.3
    table_ratio_threshold: float = 0.3
    list_ratio_threshold: float = 0.4


# ---------------------------------------------------------------------------
# Параметры загрузки
# ---------------------------------------------------------------------------

@dataclass
class SpaceLoadParams:
    """Параметры загрузки пространства Confluence."""
    api_page_limit: int = 50
    max_pages: Optional[int] = None


@dataclass
class StorageParams:
    """Параметры сохранения в ChromaDB."""
    batch_size: int = 32


BATCH_SIZE_OPTIONS = [8, 16, 32, 64, 128]


# ---------------------------------------------------------------------------
# Промпты
# ---------------------------------------------------------------------------

DEFAULT_SYSTEM_PROMPT = (
    "Ты — эксперт по корпоративной базе знаний. "
    "Отвечай ТОЛЬКО по предоставленному контексту, не ищи ничего в интернете."
)
DEFAULT_USER_TEMPLATE = "Контекст:\n{context}\n\nВопрос: {query}\n\nДай чёткий ответ."


@dataclass
class PromptParams:
    """Шаблоны промптов, управляемые пользователем."""
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    user_template: str = DEFAULT_USER_TEMPLATE

    def format_user_message(self, context: str, query: str) -> str:
        """Подставить контекст и вопрос в шаблон."""
        return self.user_template.format(context=context, query=query)


# ---------------------------------------------------------------------------
# Сообщение чата
# ---------------------------------------------------------------------------

@dataclass
class ChatMessage:
    question: str
    answer: str
    thinking: str = ""
    rag_context: str = ""
