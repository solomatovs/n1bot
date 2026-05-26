"""Проверки валидации параметров Confluence перед запуском ingest."""

import pytest

from langgraph_rag.config import Settings
from langgraph_rag.ingest import ConfluenceIngestPipeline


class TestConfluenceIngestValidation:
    @staticmethod
    def _make_settings(**overrides: object) -> Settings:
        defaults: dict[str, object] = {
            "pg_dsn": "postgresql+psycopg://u:p@h/db",
            "classifier_model": "ollama_chat/m",
            "keyword_extractor_model": "ollama_chat/m",
            "answerer_model": "ollama_chat/m",
        }
        defaults.update(overrides)
        return Settings(_env_file=None, **defaults)

    def test_run_raises_when_confluence_credentials_missing(self) -> None:
        settings = self._make_settings(confluence_space_keys=["SPACE"])
        pipeline = ConfluenceIngestPipeline(settings)
        with pytest.raises(RuntimeError, match="LGRAG_CONFLUENCE_URL"):
            pipeline.run(recreate=False)

    def test_run_raises_when_space_keys_empty(self) -> None:
        settings = self._make_settings(
            confluence_url="https://x.atlassian.net/wiki",
            confluence_username="u",
            confluence_api_token="t",
            confluence_space_keys=[],
        )
        pipeline = ConfluenceIngestPipeline(settings)
        with pytest.raises(RuntimeError, match="LGRAG_CONFLUENCE_SPACE_KEYS"):
            pipeline.run(recreate=False)
