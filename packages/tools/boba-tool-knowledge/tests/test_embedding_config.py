"""Конфиг приложения: embedding-секции kb/ingest должны оставаться валидными."""

from __future__ import annotations

import pytest
from pydantic import RootModel

from boba.llm.embedding import EmbeddingConfig, LocalEmbedding

MAX_REASONABLE_BATCH = 16
"""Выше этого инференс e5-large перестаёт помещаться в лимит профиля kb."""


class _Embedding(RootModel[EmbeddingConfig]):
    """Holder: bind принимает BaseModel, union-псевдоним — нет."""


class TestConfigKeepsBatchSmall:
    """Конфиг приложения: батч локальной модели должен помещаться в память."""

    @pytest.mark.parametrize("section", ["tool.kb", "tool.ingest"])
    def test_batch_size_is_bounded(self, raw_config, section: str) -> None:
        from boba.settings import bind

        embedding = bind(
            raw_config, path=f"{section}.embedding", model=_Embedding
        ).root

        if not isinstance(embedding, LocalEmbedding):
            return

        if not (0 < embedding.batch_size <= MAX_REASONABLE_BATCH):
            raise AssertionError(
                f"[{section}.embedding]: batch_size={embedding.batch_size} — "
                "инференс ONNX растёт линейно по батчу и словит OOM"
            )
