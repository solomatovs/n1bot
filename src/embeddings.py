from __future__ import annotations

from typing import List, Optional

import httpx
from langchain.embeddings.base import Embeddings
from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr

from config import EMBEDDING_MODEL, EMBEDDING_TIMEOUT


class LiteLLMEmbeddings(Embeddings):
    """Кастомный класс эмбеддингов для liteLLM через httpx."""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str = "unused",
        timeout: Optional[int] = None,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = httpx.Client(
            verify=False,
            timeout=float(timeout if timeout is not None else EMBEDDING_TIMEOUT),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    def _embed(self, texts: List[str], prefix: str = "") -> List[List[float]]:
        if "e5" in self.model.lower():
            texts = [f"{prefix}{t}" for t in texts]

        response = self.client.post(
            f"{self.base_url}/v1/embeddings",
            json={"model": self.model, "input": texts},
        )
        if response.status_code != 200:
            raise Exception(f"Error from liteLLM: {response.status_code} - {response.text}")

        data = response.json()
        return [item["embedding"] for item in data["data"]]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self._embed(texts, prefix="passage: ")

    def embed_query(self, text: str) -> List[float]:
        return self._embed([text], prefix="query: ")[0]

    def __del__(self) -> None:
        if hasattr(self, "client"):
            self.client.close()


class E5OllamaEmbeddings(OpenAIEmbeddings):
    """Адаптер для liteLLM с префиксами E5."""

    def __init__(
        self,
        model: str = EMBEDDING_MODEL,
        base_url: Optional[str] = None,
        api_key: str = "unused",
        **kwargs,
    ):
        base = (base_url or "").rstrip("/")
        super().__init__(
            model=model,
            base_url=f"{base}/v1",
            api_key=SecretStr(api_key),
            **kwargs,
        )

    def embed_query(self, text: str):
        return super().embed_query(f"query: {text}")

    def embed_documents(self, texts: List[str]):
        return super().embed_documents([f"passage: {t}" for t in texts])
