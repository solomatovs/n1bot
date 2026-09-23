"""Конфиг приложения: embedding-секции kb/ingest должны оставаться валидными."""

from __future__ import annotations

import pytest

from boba.llm.fastembed import FastembedProvider
from boba.llm.providers import EmbeddingModelConfig

MAX_REASONABLE_BATCH = 16
"""Выше этого инференс e5-large перестаёт помещаться в лимит профиля kb."""


class TestConfigKeepsBatchSmall:
    """Конфиг приложения: батч локальной модели должен помещаться в память."""

    @pytest.mark.parametrize("section", ["tool.kb", "tool.ingest"])
    def test_batch_size_is_bounded(self, raw_config, section: str) -> None:
        from boba.config import bind

        embedding = bind(
            raw_config, path=f"{section}.embedding", model=EmbeddingModelConfig
        )

        if not isinstance(embedding.provider, FastembedProvider):
            return

        if not (0 < embedding.batch_size <= MAX_REASONABLE_BATCH):
            raise AssertionError(
                f"[{section}.embedding]: batch_size={embedding.batch_size} — "
                "инференс ONNX растёт линейно по батчу и словит OOM"
            )
