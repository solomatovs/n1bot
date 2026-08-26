"""OpenAiEmbedder: контракт openai-совместимого /embeddings поверх httpx."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from boba.chat.openai import OpenAiConfig
from boba.llm.embedding import (
    EmbedderFactory,
    EmbeddingError,
    OpenAiEmbedder,
    OpenAiEmbedding,
)

pytestmark = pytest.mark.anyio

DIM = 3


def _patch(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_client = httpx.AsyncClient

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("boba.llm.openai.httpx.AsyncClient", mock_client)


def _config(batch_size: int) -> OpenAiEmbedding:
    return OpenAiEmbedding(
        provider="openai",
        model="text-embedding-test",
        dim=DIM,
        batch_size=batch_size,
        progress_every=100,
        openai=OpenAiConfig(base_url="https://llm.test/v1", api_key="secret-key"),
    )


def _reply(
    vectors: list[list[float]],
    order: list[int] | None = None,
) -> httpx.Response:
    indexes = order
    if indexes is None:
        indexes = list(range(len(vectors)))

    data: list[dict[str, Any]] = []
    for index, vector in zip(indexes, vectors, strict=True):
        data.append({"index": index, "embedding": vector})

    return httpx.Response(200, json={"object": "list", "data": data})


class _Recorder:
    """Хэндлер MockTransport: запоминает запросы, отвечает по input."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        self.headers.append(dict(request.headers))

        vectors: list[list[float]] = []
        for position, _text in enumerate(payload["input"]):
            vectors.append([float(position)] * DIM)

        return _reply(vectors)


async def test_documents_go_in_configured_batches(monkeypatch) -> None:
    recorder = _Recorder()
    _patch(monkeypatch, recorder)

    embedder = EmbedderFactory.build(_config(2))
    vectors = await embedder.embed_documents(["a", "b", "c", "d", "e"])

    inputs = [r["input"] for r in recorder.requests]
    if inputs != [["a", "b"], ["c", "d"], ["e"]]:
        raise AssertionError('inputs == [["a", "b"], ["c", "d"], ["e"]]')

    if len(vectors) != 5:
        raise AssertionError("len(vectors) == 5")


async def test_request_carries_model_auth_and_url(monkeypatch) -> None:
    recorder = _Recorder()
    _patch(monkeypatch, recorder)

    embedder = OpenAiEmbedder(_config(2))
    await embedder.embed_query("вопрос")

    if recorder.requests != [{"model": "text-embedding-test", "input": ["вопрос"]}]:
        raise AssertionError("recorder.requests: model+input")

    if recorder.headers[0]["authorization"] != "Bearer secret-key":
        raise AssertionError('headers["authorization"] == "Bearer secret-key"')


async def test_vectors_come_back_in_input_order(monkeypatch) -> None:
    """Провайдер может отдать data не по порядку — сортировка по index обязана."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return _reply([[1.0] * DIM, [0.0] * DIM], order=[1, 0])

    _patch(monkeypatch, handler)

    embedder = OpenAiEmbedder(_config(2))
    vectors = await embedder.embed_documents(["первый", "второй"])

    if list(vectors[0]) != [0.0] * DIM:
        raise AssertionError("vectors[0] == [0.0] * DIM")

    if list(vectors[1]) != [1.0] * DIM:
        raise AssertionError("vectors[1] == [1.0] * DIM")


async def test_wrong_dim_is_an_embedding_error(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _reply([[0.0] * (DIM + 1)])

    _patch(monkeypatch, handler)

    embedder = OpenAiEmbedder(_config(2))
    with pytest.raises(EmbeddingError, match="dim"):
        await embedder.embed_query("вопрос")


async def test_http_failure_is_an_embedding_error(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    _patch(monkeypatch, handler)

    embedder = OpenAiEmbedder(_config(2))
    with pytest.raises(EmbeddingError, match="endpoint failed"):
        await embedder.embed_query("вопрос")


async def test_malformed_body_is_an_embedding_error(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    _patch(monkeypatch, handler)

    embedder = OpenAiEmbedder(_config(2))
    with pytest.raises(EmbeddingError, match="malformed"):
        await embedder.embed_query("вопрос")


async def test_count_mismatch_is_an_embedding_error(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _reply([[0.0] * DIM])

    _patch(monkeypatch, handler)

    embedder = OpenAiEmbedder(_config(2))
    with pytest.raises(EmbeddingError, match="vectors"):
        await embedder.embed_documents(["первый", "второй"])
