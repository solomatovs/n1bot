"""AppProvider — singleton-сервисы приложения (Scope.APP)."""
from __future__ import annotations

import ssl
import warnings

import httpx
import urllib3
from dishka import Provider, Scope, provide
from openai import OpenAI

from adapters.confluence_importer import ConfluenceImporter
from adapters.litellm_embeddings import LiteLLMEmbeddings
from application.readers.html import HtmlReader
from application.readers.markdown import MarkdownReader
from application.readers.registry import DocumentReaderRegistry
from domain.config import AppConfig
from domain.di_types import EmbeddingModel
from domain.importing.confluence import ConfluenceImportFactory, ConfluenceImportService
from domain.importing.loading import ConfluenceImportParams


class AppProvider(Provider):
    """Singleton-сервисы: конфигурация, LLM-клиент, эмбеддинги."""
    scope = Scope.APP

    @provide
    def config(self) -> AppConfig:
        return AppConfig()

    @provide
    def embedding_model(self, cfg: AppConfig) -> EmbeddingModel:
        return EmbeddingModel(cfg.embedding_model)

    @provide
    def openai_client(self, cfg: AppConfig) -> OpenAI:
        _configure_ssl()
        http_client = httpx.Client(
            verify=cfg.ssl_verify,
            timeout=float(cfg.llm_timeout),
            headers={**cfg.litellm_auth_headers, "Content-Type": "application/json"},
        )
        return OpenAI(
            base_url=cfg.openai_url,
            api_key=cfg.litellm_api_key,
            http_client=http_client,
        )

    @provide
    def reader_registry(self) -> DocumentReaderRegistry:
        reg = DocumentReaderRegistry()
        reg.register(MarkdownReader())
        reg.register(HtmlReader())
        return reg

    @provide
    def confluence_import_factory(self, cfg: AppConfig) -> ConfluenceImportFactory:
        def factory(params: ConfluenceImportParams) -> ConfluenceImportService:
            return ConfluenceImporter(cfg, params)
        return factory

    @provide
    def embeddings(self, cfg: AppConfig) -> LiteLLMEmbeddings:
        return LiteLLMEmbeddings(
            model=cfg.embedding_model,
            base_url=cfg.litellm_base_url,
            api_key=cfg.litellm_api_key,
            timeout=cfg.embedding_timeout,
            ssl_verify=cfg.ssl_verify,
            auth_headers=cfg.litellm_auth_headers,
        )


def _configure_ssl() -> None:
    """Отключить проверку SSL глобально (корпоративная среда)."""
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    try:
        ssl._create_default_https_context = ssl._create_unverified_context  # type: ignore[assignment]
    except AttributeError:
        pass
