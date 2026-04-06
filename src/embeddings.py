"""Эмбеддинги через liteLLM (OpenAI-совместимый API)."""
from __future__ import annotations

from typing import List

import httpx
from langchain.embeddings.base import Embeddings


class LiteLLMEmbeddings(Embeddings):
    """Кастомный класс эмбеддингов для liteLLM через httpx."""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        timeout: int,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = httpx.Client(
            verify=False,
            timeout=float(timeout),
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
            raise ConnectionError(f"LiteLLM error: {response.status_code} - {response.text}")

        data = response.json()
        return [item["embedding"] for item in data["data"]]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self._embed(texts, prefix="passage: ")

    def embed_query(self, text: str) -> List[float]:
        return self._embed([text], prefix="query: ")[0]

    def __del__(self) -> None:
        if hasattr(self, "client"):
            self.client.close()
