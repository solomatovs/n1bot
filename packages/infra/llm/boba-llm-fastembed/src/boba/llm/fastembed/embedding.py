"""Локальные эмбеддинги fastembed (in-process, ONNX).

Модель грузится один раз на процесс и на конфиг использования: бэкенд
держит загруженные экземпляры, повторный запрос той же модели отдаёт их.
В зиготе инструмента прогрев кладёт модель до готовности, вызов приходит
форком и берёт её же через copy-on-write вместо повторной загрузки; счёт в
форке однопоточный (рабочие потоки движка fork не переживают).

Ошибки:
LlmError — fastembed не установлен, модель не загрузилась или вернула вектор
    чужой размерности.
LlmProvidersError — секция не того провайдера или у провайдера нет чата.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import warnings
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from typing import ClassVar, Literal

from pydantic import Field

from boba.llm.chat import ChatModel, LlmError
from boba.llm.embedding import EmbeddingModel
from boba.llm.providers import (
    ChatModelConfig,
    EmbeddingModelConfig,
    LlmBackend,
    LlmProvider,
    LlmProviderManifest,
    LlmProvidersError,
)
from boba.toolkit.timing import Elapsed

logger = logging.getLogger(__name__)

__all__ = [
    "MANIFEST",
    "FastembedBackend",
    "FastembedEmbeddingModel",
    "FastembedProvider",
]


class FastembedProvider(LlmProvider):
    """Секция `[llm.<имя>]` с kind = "fastembed": кэш весов."""

    kind: Literal["fastembed"]

    cache_dir: str = Field(
        description="Каталог HF/ONNX-весов fastembed; модели кладутся заранее.",
    )


class FastembedEmbeddingModel(EmbeddingModel):
    """Реализация EmbeddingModel на fastembed; префиксы e5 подставляет fastembed.

    Модель одна на процесс, поэтому инференс уходит в поток под локом: loop
    остаётся свободен, а ONNX внутри и так занимает все выделенные ядра.
    """

    DISTRIBUTION: ClassVar[str] = "fastembed"
    POOLING_WARNING: ClassVar[str] = "now uses mean pooling instead of CLS embedding"
    """Сообщение fastembed о смене пулинга у моделей e5: проект работает на
    текущем пулинге, поэтому предупреждение гасится, а версия пакета уходит в
    лог — вектор другой версии fastembed с нынешним запросом несравним."""

    def __init__(self, provider: FastembedProvider, cfg: EmbeddingModelConfig) -> None:
        imports = Elapsed()
        try:
            from fastembed import (  # noqa: PLC0415 # pyright: ignore[reportMissingImports]
                TextEmbedding,
            )
        except ImportError as exc:
            msg = f"fastembed embedding: importing {self.DISTRIBUTION} failed: {exc}"
            raise LlmError(msg) from exc

        logger.info(
            "embedder: fastembed %s imported in %dms",
            self._fastembed_version(),
            imports.ms(),
        )

        # размер пула onnxruntime берёт из маски доступных ядер, а её
        # выставляет запуск по cgroup-квоте профиля
        cores = len(os.sched_getaffinity(0))
        logger.info("embedder: %s on %d core(s)", cfg.model, cores)

        load = Elapsed()
        self._cfg = cfg
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message=f".*{self.POOLING_WARNING}.*", category=UserWarning
            )
            try:
                self._model = TextEmbedding(
                    model_name=cfg.model,
                    cache_dir=provider.cache_dir,
                )
            except Exception as exc:
                msg = (
                    f"fastembed embedding: loading {cfg.model!r} from "
                    f"{provider.cache_dir} failed: {type(exc).__name__}: {exc}"
                )
                raise LlmError(msg) from exc
        logger.info("embedder: %s loaded in %dms", cfg.model, load.ms())

        self._lock = threading.Lock()

        # захваченный в момент fork замок остался бы захваченным в ребёнке
        # навсегда: владелец в ребёнка не переносится
        os.register_at_fork(after_in_child=self._reset_lock)

    @classmethod
    def _fastembed_version(cls) -> str:
        """Версия пакета, которым посчитан индекс; в тестах модуль подменён
        заглушкой, и дистрибутива может не быть."""
        try:
            return version(cls.DISTRIBUTION)
        except PackageNotFoundError:
            return "unknown"

    def _reset_lock(self) -> None:
        self._lock = threading.Lock()

    async def embed_documents(
        self,
        contents: Sequence[str],
    ) -> Sequence[Sequence[float]]:
        return await asyncio.to_thread(self._locked_passage_embed, contents)

    async def embed_query(self, content: str) -> Sequence[float]:
        return await asyncio.to_thread(self._locked_query_embed, content)

    def dim(self) -> int:
        return self._cfg.dim

    def _locked_passage_embed(self, contents: Sequence[str]) -> list[Sequence[float]]:
        with self._lock:
            return self._passage_embed(contents)

    def _locked_query_embed(self, content: str) -> Sequence[float]:
        with self._lock:
            return self._query_embed(content)

    def _passage_embed(self, contents: Sequence[str]) -> list[Sequence[float]]:
        vectors: list[Sequence[float]] = []
        elapsed = Elapsed()
        for vec in self._model.passage_embed(contents, batch_size=self._cfg.batch_size):
            v = vec.tolist()
            self._check_dim(v)
            vectors.append(v)

            if len(vectors) % self._cfg.progress_every == 0:
                logger.info(
                    "embedding progress: %d/%d in %dms",
                    len(vectors),
                    len(contents),
                    elapsed.ms(),
                )

        return vectors

    def _query_embed(self, content: str) -> Sequence[float]:
        elapsed = Elapsed()
        gen = self._model.query_embed([content], batch_size=self._cfg.batch_size)
        vec = next(iter(gen))
        v = vec.tolist()
        self._check_dim(v)

        logger.info("embedder: query inference in %dms", elapsed.ms())

        return v

    def _check_dim(self, vec: Sequence[float]) -> None:
        actual = len(vec)
        if actual != self._cfg.dim:
            msg = (
                f"fastembed embedding: model {self._cfg.model!r} returned "
                f"vectors of dim {actual}, config declares {self._cfg.dim}"
            )
            raise LlmError(msg)


class FastembedBackend(LlmBackend):
    """Реализация LlmBackend: загруженные модели по конфигу использования."""

    def __init__(self, provider: LlmProvider) -> None:
        if not isinstance(provider, FastembedProvider):
            msg = (
                f"fastembed backend expects a FastembedProvider section, "
                f"got kind {provider.kind!r}"
            )
            raise LlmProvidersError(msg)

        self._provider = provider
        self._loaded: list[tuple[EmbeddingModelConfig, FastembedEmbeddingModel]] = []

    def chat(self, cfg: ChatModelConfig) -> ChatModel:
        msg = (
            f"fastembed provider at {self._provider.cache_dir} has no chat "
            f"models, asked for {cfg.model!r}"
        )
        raise LlmProvidersError(msg)

    def embedding(self, cfg: EmbeddingModelConfig) -> EmbeddingModel:
        for known, model in self._loaded:
            if known == cfg:
                return model

        model = FastembedEmbeddingModel(self._provider, cfg)
        self._loaded.append((cfg, model))

        return model

    async def aclose(self) -> None:
        self._loaded.clear()


MANIFEST = LlmProviderManifest(
    kind="fastembed",
    config=FastembedProvider,
    backend=FastembedBackend,
)
