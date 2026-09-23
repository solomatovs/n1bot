"""OpenAiEmbeddingModel: контракт /embeddings поверх HttpTransport."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from boba.llm.chat import LlmError
from boba.llm.embedding import EmbeddingModel
from boba.llm.http.openai import OpenAiBackend, OpenAiProvider
from boba.llm.providers import EmbeddingModelConfig
from boba.transport.http import HttpTransportConfig
from boba.transport.http.connection import BearerAuth, HttpConnection

pytestmark = pytest.mark.anyio

DIM = 3

Handler = Callable[[httpx.Request], httpx.Response]


def _patch(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    real_client = httpx.AsyncClient

    def mock_client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("boba.transport.http.transport.httpx.AsyncClient", mock_client)


def _embedder(
    monkeypatch: pytest.MonkeyPatch, handler: Handler, batch_size: int
) -> EmbeddingModel:
    _patch(monkeypatch, handler)
    provider = OpenAiProvider(
        kind="openai",
        connection=HttpConnection(
            host="llm.test",
            path="/v1",
            auth=BearerAuth(method="bearer", token=SecretStr("secret-key")),
        ),
        transport=HttpTransportConfig(),
    )
    cfg = EmbeddingModelConfig(
        provider=provider,
        model="text-embedding-test",
        dim=DIM,
        batch_size=batch_size,
        progress_every=100,
    )

    return OpenAiBackend(provider).embedding(cfg)


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
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        self.headers.append(dict(request.headers))
        self.urls.append(str(request.url))

        vectors: list[list[float]] = []
        for position, _text in enumerate(payload["input"]):
            vectors.append([float(position)] * DIM)

        return _reply(vectors)


async def test_documents_go_in_configured_batches(monkeypatch) -> None:
    recorder = _Recorder()

    embedder = _embedder(monkeypatch, recorder, 2)
    vectors = await embedder.embed_documents(["a", "b", "c", "d", "e"])

    inputs = [r["input"] for r in recorder.requests]
    if inputs != [["a", "b"], ["c", "d"], ["e"]]:
        raise AssertionError('inputs == [["a", "b"], ["c", "d"], ["e"]]')

    if len(vectors) != 5:
        raise AssertionError("len(vectors) == 5")


async def test_request_carries_model_auth_and_url(monkeypatch) -> None:
    recorder = _Recorder()

    embedder = _embedder(monkeypatch, recorder, 2)
    await embedder.embed_query("вопрос")

    if recorder.requests != [{"model": "text-embedding-test", "input": ["вопрос"]}]:
        raise AssertionError("recorder.requests: model+input")

    if recorder.headers[0]["authorization"] != "Bearer secret-key":
        raise AssertionError('headers["authorization"] == "Bearer secret-key"')

    if recorder.urls[0] != "https://llm.test/v1/embeddings":
        raise AssertionError(recorder.urls)


async def test_vectors_come_back_in_input_order(monkeypatch) -> None:
    """Провайдер может отдать data не по порядку — сортировка по index обязана."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return _reply([[1.0] * DIM, [0.0] * DIM], order=[1, 0])

    embedder = _embedder(monkeypatch, handler, 2)
    vectors = await embedder.embed_documents(["первый", "второй"])

    if list(vectors[0]) != [0.0] * DIM:
        raise AssertionError("vectors[0] == [0.0] * DIM")

    if list(vectors[1]) != [1.0] * DIM:
        raise AssertionError("vectors[1] == [1.0] * DIM")


async def test_wrong_dim_is_an_llm_error(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _reply([[0.0] * (DIM + 1)])

    embedder = _embedder(monkeypatch, handler, 2)
    with pytest.raises(LlmError, match="dim"):
        await embedder.embed_query("вопрос")


async def test_http_failure_is_an_llm_error(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"denied")

    embedder = _embedder(monkeypatch, handler, 2)
    with pytest.raises(LlmError, match="401"):
        await embedder.embed_query("вопрос")


async def test_malformed_body_is_an_llm_error(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    embedder = _embedder(monkeypatch, handler, 2)
    with pytest.raises(LlmError, match="embeddings reply"):
        await embedder.embed_query("вопрос")


async def test_count_mismatch_is_an_llm_error(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _reply([[0.0] * DIM])

    embedder = _embedder(monkeypatch, handler, 2)
    with pytest.raises(LlmError, match="1 vectors for 2 inputs"):
        await embedder.embed_documents(["a", "b"])
