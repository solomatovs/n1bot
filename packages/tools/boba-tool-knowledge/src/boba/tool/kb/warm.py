"""Прогретый эмбеддер модулей kb: кладёт хук прогрева, читают тела вызовов.

Кэш держит инструмент, а не библиотека: что и когда кэшировать, решает автор
инструмента, а не общая фабрика за его спиной. В зиготе хук прогрева кладёт
сюда модель до готовности, вызов приходит форком и берёт её же через
copy-on-write вместо повторной загрузки.

Счёт в форке однопоточный: рабочие потоки нативного движка fork не переживают
(в ребёнка попадает только вызвавший поток). Для одного запроса это дешевле
повторной загрузки модели, батчи документов через прогретый экземпляр гонять
не стоит.

Ошибки: наружу не выпускает.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from boba.indexing.ports import Embedder
from boba.llm.embedding import EmbedderFactory, EmbeddingConfig, LocalEmbedding
from boba.toolkit.timing import Elapsed

__all__ = ["WarmEmbedder"]

logger = logging.getLogger(__name__)


class WarmEmbedder:
    """Экземпляр эмбеддера, переживающий вызовы: один на процесс инструмента."""

    _warm: ClassVar[tuple[LocalEmbedding, Embedder[str]] | None] = None

    @classmethod
    def load(cls, cfg: EmbeddingConfig) -> Embedder[str]:
        """Собрать эмбеддер и запомнить его; зовётся хуком прогрева зиготы.

        Запоминается только локальная модель: её загрузка стоит секунду и
        полтора гигабайта. Удалённый эмбеддер состояния не имеет, держать его
        между вызовами нечего — и его конфиг с ключом API в памяти не оседает.
        """
        built = EmbedderFactory.build(cfg)

        if not isinstance(cfg, LocalEmbedding):
            return built

        elapsed = Elapsed()
        cls._warm = (cfg, built)
        logger.info("embedder warmed: %s in %dms", cfg.model, elapsed.ms())

        return built

    @classmethod
    def of(cls, cfg: EmbeddingConfig) -> Embedder[str]:
        """Прогретый эмбеддер для этой конфигурации либо собранный сейчас."""
        warm = cls._warm
        if warm is None:
            return EmbedderFactory.build(cfg)

        warm_cfg, embedder = warm
        if warm_cfg != cfg:
            return EmbedderFactory.build(cfg)

        return embedder

    @classmethod
    def forget(cls) -> None:
        """Забыть прогретый экземпляр; нужен тестам, меняющим конфигурацию."""
        cls._warm = None
