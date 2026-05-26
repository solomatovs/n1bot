"""Embeddings-обёртка для intfloat/multilingual-e5-*: подставляет префиксы query/passage."""

from pathlib import Path
from typing import Any, ClassVar

from langchain_core.embeddings import Embeddings
from langchain_huggingface import HuggingFaceEmbeddings


class E5Embeddings(Embeddings):
    """Embeddings под семейство multilingual-e5: добавляет обязательные префиксы перед энкодингом."""

    _QUERY_PREFIX: ClassVar[str] = "query: "
    _PASSAGE_PREFIX: ClassVar[str] = "passage: "

    def __init__(
        self, model_name: str, device: str, cache_dir: Path | None
    ) -> None:
        kwargs: dict[str, Any] = {
            "model_name": model_name,
            "model_kwargs": {"device": device},
            "encode_kwargs": {"normalize_embeddings": True, "show_progress_bar": True},
            "show_progress": True,
        }
        if cache_dir is not None:
            kwargs["cache_folder"] = str(cache_dir)
        self._backend = HuggingFaceEmbeddings(**kwargs)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        prefixed = [f"{self._PASSAGE_PREFIX}{text}" for text in texts]
        return self._backend.embed_documents(prefixed)

    def embed_query(self, text: str) -> list[float]:
        return self._backend.embed_query(f"{self._QUERY_PREFIX}{text}")
