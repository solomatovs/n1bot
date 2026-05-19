"""
Embedder factory для ingest-пути.

Read-side (kb_search) использует chromadb-native `EmbeddingFunction` напрямую
(через `Collection.query(query_texts=...)` — chromadb сам зовёт EF). Ingest-
пути нужен `boba.indexing.Embedder[str]` — тот же EF, но завёрнутый в
provider-нейтральный интерфейс boba.indexing, который понимает
`ChromaVectorStore.upsert(...)`.

Контракт `EmbedderFactory.create`: `""` (не задано) → ошибка; `"default"`
(явно выбрано) → built-in ONNX; иначе → OpenAI-совместимый endpoint.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Any, ClassVar

from boba.indexing.embedder import Embedder

__all__ = [
    "ChromaEmbeddingFunctionAdapter",
    "EmbedderFactory",
    "EmbeddingModelNotConfiguredError",
    "build_chromadb_embedding_function",
]


def build_chromadb_embedding_function(
    *,
    model: str,
    base_url: str,
    api_key: str,
) -> Any:
    """Chromadb-native `EmbeddingFunction` для read-side kb-tools.

    Read-side (kb_search) использует `Collection.query(query_texts=...)` —
    chromadb сам зовёт EF на стороне коллекции, поэтому нужен EF, а не
    `Embedder[str]`. Routing совпадает с `EmbedderFactory.create`:
    - `""` / `"default"` → None (chromadb использует built-in ONNX);
    - иначе → `OpenAIEmbeddingFunction` поверх base_url/api_key.

    `None` означает «chromadb сам подставит DefaultEmbeddingFunction».
    """
    if model in ("", "default"):
        return None
    from chromadb.utils.embedding_functions import (  # noqa: PLC0415
        OpenAIEmbeddingFunction,
    )

    return OpenAIEmbeddingFunction(
        api_key=api_key or "unused",
        api_base=base_url or None,
        model_name=model,
    )


class EmbeddingModelNotConfiguredError(ValueError):
    """Поднимается, когда `embedding_model` пуст (не задан в конфиге).

    Явный `'default'` в конфиге разрешён (=built-in ONNX all-MiniLM-L6-v2,
    оффлайн, без сети — удобно для тестов / dev-окружений). Запрет
    действует только на пустую строку: это означает, что оператор не
    задумывался о выборе и tool падает fail-fast вместо тихого fallback'а.
    """


class ChromaEmbeddingFunctionAdapter(Embedder[str]):
    """Адаптер `chromadb.EmbeddingFunction` → `boba.indexing.Embedder[str]`.

    Chromadb `EmbeddingFunction.__call__(input: Documents) -> Embeddings`
    — синхронный батч-вызов. Adapter:
    - `embed_documents(contents)` — батч-вызов EF на весь итератор;
    - `embed_query(content)` — однострочный батч (одна строка);
    - `dim()` — кэшируется при первом успешном embed (lazy probe).

    Asymmetric document/query prefixes (e.g., e5/bge): chromadb EF их не
    знает — если модель требует префиксы, заворачивай EF в кастом и
    передавай уже его сюда (или используй родной `Embedder` из
    `boba-openai`/HF и обходись без этого адаптера).
    """

    def __init__(self, embedding_function: Any) -> None:
        self._ef = embedding_function
        self._dim: int | None = None

    def embed_documents(
        self, contents: Iterable[str],
    ) -> Iterator[Sequence[float]]:
        texts = list(contents)
        if not texts:
            return
        embeddings = self._ef(texts)
        if self._dim is None and len(embeddings):
            self._dim = len(embeddings[0])
        for vec in embeddings:
            # Принудительный cast в native float: ONNX EF возвращает
            # numpy.float32, а chromadb upsert валидирует embeddings
            # на list[float] | list[ndarray] и `list[np.float32]` не
            # принимает (видит как `list[list[scalar]]` без флага ndarray).
            yield [float(x) for x in vec]

    def embed_query(self, content: str) -> Sequence[float]:
        embeddings = self._ef([content])
        if len(embeddings) == 0:
            msg = "EmbeddingFunction returned empty result for query"
            raise RuntimeError(msg)
        vec = [float(x) for x in embeddings[0]]
        if self._dim is None:
            self._dim = len(vec)
        return vec

    def dim(self) -> int:
        if self._dim is None:
            # Lazy probe: маленький запрос для определения размерности.
            self.embed_query("dim-probe")
        assert self._dim is not None
        return self._dim


class EmbedderFactory:
    """Фабрика `Embedder[str]` для ingest-пути ChromaDB-тула.

    Один экземпляр на плагин: тащит политику валидации модели + impл-выбор
    chromadb-EF. Параметры берёт per-call в `create(...)`, не хранит — это
    позволяет один и тот же factory переиспользовать с разными конфигами
    (тесты, multi-tenant).
    """

    _DEFAULT_MODEL_NAME: ClassVar[str] = "default"

    def create(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
    ) -> Embedder[str]:
        """Построить `Embedder[str]` по параметрам конфига.

        Routing:
        - `""`        → `EmbeddingModelNotConfiguredError` (не задано).
        - `"default"` → built-in chromadb `DefaultEmbeddingFunction`
          (ONNX all-MiniLM-L6-v2, оффлайн, ~80MB модель скачивается при
          первом импорте; полезно для тестов / dev без LiteLLM/OpenAI proxy).
        - иначе       → `OpenAIEmbeddingFunction` поверх указанного
          base_url/api_key (LiteLLM proxy / OpenAI / vLLM).
        """
        if model == "":
            msg = (
                "embedding_model is empty; set [tool.chromadb] "
                "embedding_model in config (e.g. 'multilingual-e5-large' "
                "для русскоязычной KB, или 'default' для built-in ONNX "
                "без сети)."
            )
            raise EmbeddingModelNotConfiguredError(msg)

        if model == self._DEFAULT_MODEL_NAME:
            return self._build_default()

        return self._build_openai_compatible(
            model=model,
            base_url=base_url,
            api_key=api_key,
        )

    @staticmethod
    def _build_default() -> Embedder[str]:
        from chromadb.utils.embedding_functions import (  # noqa: PLC0415
            DefaultEmbeddingFunction,
        )

        return ChromaEmbeddingFunctionAdapter(DefaultEmbeddingFunction())

    @staticmethod
    def _build_openai_compatible(
        *,
        model: str,
        base_url: str,
        api_key: str,
    ) -> Embedder[str]:
        from chromadb.utils.embedding_functions import (  # noqa: PLC0415
            OpenAIEmbeddingFunction,
        )

        ef = OpenAIEmbeddingFunction(
            api_key=api_key or "unused",
            api_base=base_url or None,
            model_name=model,
        )
        return ChromaEmbeddingFunctionAdapter(ef)
