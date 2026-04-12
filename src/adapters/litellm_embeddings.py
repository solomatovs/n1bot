"""Эмбеддинги через liteLLM (OpenAI-совместимый API)."""
from __future__ import annotations

import time
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
        ssl_verify: bool = False,
        auth_headers: dict[str, str] | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        headers = {
            **(auth_headers or {"Authorization": f"Bearer {api_key}"}),
            "Content-Type": "application/json",
        }
        self.client = httpx.Client(
            verify=ssl_verify,
            timeout=float(timeout),
            headers=headers,
        )

    def _embed(self, texts: List[str], prefix: str = "") -> List[List[float]]:
        if "e5" in self.model.lower():
            texts = [f"{prefix}{t}" for t in texts]

        url = f"{self.base_url}/v1/embeddings"
        payload = {"model": self.model, "input": texts}
        start = time.monotonic()

        try:
            response = self.client.post(url, json=payload)
        except httpx.TimeoutException as e:
            elapsed = time.monotonic() - start
            raise ConnectionError(
                f"Embedding request timed out after {elapsed:.1f}s "
                f"(timeout={self.timeout}s). "
                f"URL: {url}, model: {self.model}, texts: {len(texts)}"
            ) from e

        elapsed = time.monotonic() - start

        if response.status_code != 200:
            raise ConnectionError(
                f"Embedding error {response.status_code} after {elapsed:.1f}s. "
                f"URL: {url}, model: {self.model}, texts: {len(texts)}. "
                f"Response: {response.text}"
            )

        return self._extract_embeddings(response.json())

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return self._embed(texts, prefix="passage: ")

    def embed_query(self, text: str) -> List[float]:
        return self._embed([text], prefix="query: ")[0]

    @staticmethod
    def _extract_embeddings(response_json: dict) -> List[List[float]]:
        """Извлечь векторы эмбеддингов из ответа /v1/embeddings."""
        return [item["embedding"] for item in response_json["data"]]

    def __del__(self) -> None:
        self.client.close()
