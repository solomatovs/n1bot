"""Эмбеддеры провайдеров: union-конфиг, fastembed- и openai-бэкенды, фабрика.

Ошибки:
EmbeddingError — удалённый эмбеддер недоступен, ответил мусором или вектором
    чужой размерности.
RuntimeError — локальная модель вернула вектор чужой размерности: дефект
    сборки rootfs/конфига, трейсбек по делу.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Sequence
from typing import Annotated, ClassVar, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boba.chat.http import HttpConfig
from boba.indexing.ports import Embedder
from boba.llm.http import LlmHttp
from boba.toolkit.timing import Elapsed

logger = logging.getLogger(__name__)

__all__ = [
    "EmbedderFactory",
    "EmbeddingConfig",
    "EmbeddingError",
    "LocalEmbedding",
    "LocalFastEmbedEmbedder",
    "OpenAiEmbedder",
    "OpenAiEmbedding",
]


class EmbeddingError(Exception):
    """Удалённый эмбеддинг не состоялся: сеть, статус, мусорный ответ."""


class EmbeddingBase(BaseModel):
    """Общие поля профилей embedding: model и dim ДОЛЖНЫ совпадать между
    ingest- и search-tools одной KB-коллекции, иначе silent break размерностей."""

    model: str = Field(
        description=(
            "Имя embedding-модели у провайдера. Обязательно. Должно совпадать "
            "между ingest- и search-сервисами, ходящими в одни и те же чанки "
            "kb_chunks."
        ),
    )
    progress_every: int = Field(
        gt=0,
        description=(
            "Через сколько посчитанных векторов писать строку прогресса в "
            "журнал; обязателен. 1 — строка на каждый эмбеддинг, видно, что "
            "процесс жив; больше — реже и тише."
        ),
    )
    batch_size: int = Field(
        gt=0,
        description=(
            "Сколько текстов уходит в модель за один прогон; обязателен. "
            "Локально активации ONNX растут линейно по батчу: на e5-large батч "
            "100 занимал 6.5G и ловил OOM, батч 8 — около 2.3G. Удалённо это "
            "размер input одного запроса."
        ),
    )
    dim: int = Field(
        gt=0,
        description=(
            "Размерность вектора модели; обязательна. Задаётся явно, чтобы "
            "процесс приложения не грузил модель ради одного числа. "
            "Расхождение с фактическим вектором ловится на первом эмбеддинге."
        ),
    )


class LocalEmbedding(EmbeddingBase):
    """Локальный инференс fastembed (in-process, ONNX)."""

    kind: Literal["local"]
    cache_dir: str = Field(
        default="",
        description=(
            "Каталог для HF/ONNX-весов fastembed. "
            "Пусто -> дефолт fastembed (~/.cache/fastembed)."
        ),
    )


class OpenAiEmbedding(EmbeddingBase):
    """Удалённый инференс через openai-совместимый endpoint /embeddings."""

    kind: Literal["openai"]

    http: HttpConfig = Field(
        description=(
            "Поведение HTTP-транспорта; в конфиге подключается ссылкой ${http}."
        ),
    )

    base_url: str = Field(description="Endpoint API провайдера.")

    api_key: str = Field(description="Ключ API провайдера.")


EmbeddingConfig = Annotated[
    LocalEmbedding | OpenAiEmbedding,
    Field(discriminator="kind"),
]
"""Discriminated union по kind — точная диагностика ошибок валидации."""


class LocalFastEmbedEmbedder(Embedder[str]):
    """Embedder[str] на fastembed (in-process, ONNX); префиксы e5 подставляет fastembed.

    Модель одна на процесс, поэтому инференс уходит в поток под локом: loop
    остаётся свободен для остальных источников, а ONNX внутри и так занимает
    все выделенные ядра — параллельные прогоны только отнимали бы память.

    Экземпляр переживает fork: веса достаются ребёнку через copy-on-write, но
    рабочие потоки ONNX в ребёнка не переносятся, и счёт там идёт в один
    поток (замер: 31 мс против 59 мс на один запрос). Для одиночного запроса
    это дешевле повторной загрузки модели, батчи документов лучше считать в
    процессе, который модель загрузил сам.
    """

    def __init__(self, cfg: LocalEmbedding) -> None:
        imports = Elapsed()
        from fastembed import (  # noqa: PLC0415 # pyright: ignore[reportMissingImports]
            TextEmbedding,
        )

        logger.info("embedder: fastembed imported in %dms", imports.ms())

        # размер пула onnxruntime берёт из маски доступных ядер, а её
        # выставляет запуск по cgroup-квоте профиля
        cores = len(os.sched_getaffinity(0))
        logger.info("embedder: %s on %d core(s)", cfg.model, cores)

        load = Elapsed()
        self._model_name = cfg.model
        self._model = TextEmbedding(
            model_name=cfg.model,
            cache_dir=cfg.cache_dir,
        )
        logger.info("embedder: %s loaded in %dms", cfg.model, load.ms())

        self._dim = cfg.dim
        self._batch_size = cfg.batch_size
        self._progress_every = cfg.progress_every
        self._lock = threading.Lock()

        # захваченный в момент fork замок остался бы захваченным в ребёнке
        # навсегда: владелец в ребёнка не переносится
        os.register_at_fork(after_in_child=self._reset_lock)

    def _reset_lock(self) -> None:
        self._lock = threading.Lock()

    async def embed_documents(
        self,
        contents: Sequence[str],
    ) -> Sequence[Sequence[float]]:
        return await asyncio.to_thread(self._locked_passage_embed, contents)

    async def embed_query(self, content: str) -> Sequence[float]:
        return await asyncio.to_thread(self._locked_query_embed, content)

    def _locked_passage_embed(self, contents: Sequence[str]) -> list[Sequence[float]]:
        with self._lock:
            return self._passage_embed(contents)

    def _locked_query_embed(self, content: str) -> Sequence[float]:
        with self._lock:
            return self._query_embed(content)

    def _passage_embed(self, contents: Sequence[str]) -> list[Sequence[float]]:
        vectors: list[Sequence[float]] = []
        elapsed = Elapsed()
        for vec in self._model.passage_embed(contents, batch_size=self._batch_size):
            v = vec.tolist()
            self._record_dim(v)
            vectors.append(v)

            if len(vectors) % self._progress_every == 0:
                logger.info(
                    "embedding progress: %d/%d in %dms",
                    len(vectors),
                    len(contents),
                    elapsed.ms(),
                )

        return vectors

    def _query_embed(self, content: str) -> Sequence[float]:
        elapsed = Elapsed()
        gen = self._model.query_embed([content], batch_size=self._batch_size)
        vec = next(iter(gen))
        v = vec.tolist()
        self._record_dim(v)

        logger.info("embedder: query inference in %dms", elapsed.ms())

        return v

    def dim(self) -> int:
        return self._dim

    def _record_dim(self, vec: Sequence[float]) -> None:
        actual = len(vec)
        if actual != self._dim:
            msg = (
                f"LocalFastEmbedEmbedder: model {self._model_name!r} returned "
                f"vectors of dim {actual}, config declares {self._dim}"
            )
            raise RuntimeError(msg)


class OpenAiVector(BaseModel):
    """Один вектор из data[] ответа /embeddings."""

    index: int
    embedding: list[float]


class OpenAiEmbeddingsReply(BaseModel):
    """Тело ответа /embeddings; лишние поля провайдера игнорируются."""

    model_config = ConfigDict(extra="ignore")

    data: list[OpenAiVector]


class OpenAiEmbedder(Embedder[str]):
    """Embedder[str] поверх openai-совместимого HTTP API.

    Endpoint симметричный: запрос и документы эмбеддятся одинаково, без
    префиксов. Клиент живёт на время одного embed-вызова.
    """

    ENDPOINT: ClassVar[str] = "embeddings"

    def __init__(self, cfg: OpenAiEmbedding) -> None:
        self._cfg = cfg

    async def embed_documents(
        self,
        contents: Sequence[str],
    ) -> Sequence[Sequence[float]]:
        async with LlmHttp.client(self._cfg.http) as client:
            vectors: list[Sequence[float]] = []
            elapsed = Elapsed()
            logged = 0

            for start in range(0, len(contents), self._cfg.batch_size):
                batch = list(contents[start : start + self._cfg.batch_size])
                vectors.extend(await self._request(client, batch))

                if len(vectors) - logged >= self._cfg.progress_every:
                    logged = len(vectors)
                    logger.info(
                        "embedding progress: %d/%d in %dms",
                        len(vectors),
                        len(contents),
                        elapsed.ms(),
                    )

            return vectors

    async def embed_query(self, content: str) -> Sequence[float]:
        async with LlmHttp.client(self._cfg.http) as client:
            vectors = await self._request(client, [content])
            return vectors[0]

    def dim(self) -> int:
        return self._cfg.dim

    def _endpoint(self) -> str:
        return self._cfg.base_url.rstrip("/") + "/" + self.ENDPOINT

    async def _request(
        self,
        client: httpx.AsyncClient,
        batch: Sequence[str],
    ) -> list[Sequence[float]]:
        payload = {"model": self._cfg.model, "input": list(batch)}
        headers = {"Authorization": f"Bearer {self._cfg.api_key}"}

        elapsed = Elapsed()
        try:
            response = await client.post(
                self._endpoint(), json=payload, headers=headers
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            msg = f"embeddings endpoint failed: {exc}"
            raise EmbeddingError(msg) from exc

        logger.info("embeddings request: %d inputs in %dms", len(batch), elapsed.ms())

        reply = self._parse(response.content)

        return self._vectors(reply, expected=len(batch))

    def _parse(self, body: bytes) -> OpenAiEmbeddingsReply:
        try:
            return OpenAiEmbeddingsReply.model_validate_json(body)
        except ValidationError as exc:
            msg = f"embeddings endpoint returned malformed body: {exc}"
            raise EmbeddingError(msg) from exc

    def _vectors(
        self,
        reply: OpenAiEmbeddingsReply,
        expected: int,
    ) -> list[Sequence[float]]:
        if len(reply.data) != expected:
            msg = (
                f"embeddings endpoint returned {len(reply.data)} vectors "
                f"for {expected} inputs"
            )
            raise EmbeddingError(msg)

        ordered = sorted(reply.data, key=self._index_of)

        vectors: list[Sequence[float]] = []
        for item in ordered:
            self._check_dim(item.embedding)
            vectors.append(item.embedding)

        return vectors

    @staticmethod
    def _index_of(item: OpenAiVector) -> int:
        return item.index

    def _check_dim(self, vec: Sequence[float]) -> None:
        actual = len(vec)
        if actual != self._cfg.dim:
            msg = (
                f"embeddings model {self._cfg.model!r} returned dim {actual}, "
                f"config declares {self._cfg.dim}"
            )
            raise EmbeddingError(msg)


class EmbedderFactory:
    """Собирает Embedder[str] из union-конфига.

    Каждый вызов строит новый экземпляр: держать ли собранное между вызовами —
    решает инструмент, которому это нужно, а не библиотека за его спиной.
    """

    @classmethod
    def build(cls, cfg: EmbeddingConfig) -> Embedder[str]:
        if isinstance(cfg, LocalEmbedding):
            return LocalFastEmbedEmbedder(cfg)

        return OpenAiEmbedder(cfg)
