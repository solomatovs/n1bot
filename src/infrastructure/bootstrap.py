"""Bootstrap — центральная точка сборки приложения.

Создаёт все разделяемые сервисы и пайплайны один раз.
Остальные модули получают готовые зависимости, а не создают их сами.
"""
from __future__ import annotations

import logging
import ssl
import sys
import warnings
from dataclasses import dataclass
from typing import Callable

import httpx
import urllib3
from openai import OpenAI

from adapters.chromadb_vectorstore import ChromaVectorStoreService
from adapters.confluence_importer import ConfluenceImporter
from adapters.html_converter import DocumentPreparer
from adapters.litellm_embeddings import LiteLLMEmbeddings
from domain.config import AppConfig
from domain.confluence_import import ConfluenceImportService
from domain.convert import DocumentPreparerService
from domain.loading import ConfluenceImportParams
from domain.pipeline import Pipeline
from domain.vectorstore import VectorStoreService


@dataclass
class AppServices:
    """Разделяемые сервисы приложения — создаются один раз при старте."""

    cfg: AppConfig
    openai_client: OpenAI
    vectorstore_service: VectorStoreService
    embeddings: LiteLLMEmbeddings
    query_pipeline: Pipeline
    search_pipeline: Pipeline
    load_pipeline: Pipeline
    create_vectorstore: Callable[[str], VectorStoreService]
    document_preparer: DocumentPreparerService
    create_confluence_importer: Callable[[ConfluenceImportParams], ConfluenceImportService]
    create_loading_events: Callable  # (LoadContext) -> Iterator[LoadEvent]


def bootstrap(cfg: AppConfig) -> AppServices:
    """Собрать все сервисы и пайплайны из конфигурации."""
    configure_logging(cfg)
    configure_ssl()

    embeddings = create_embeddings(cfg)
    openai_client = create_openai_client(cfg)
    vs = ChromaVectorStoreService(db_path=cfg.chroma_db_path, default_embedding=embeddings, cfg=cfg)

    def _create_vectorstore(db_path: str) -> VectorStoreService:
        return ChromaVectorStoreService(db_path=db_path, default_embedding=embeddings, cfg=cfg)

    def _create_confluence_importer(params: ConfluenceImportParams) -> ConfluenceImportService:
        return ConfluenceImporter(cfg, params)

    def _create_loading_events(ctx):  # noqa: ANN001, ANN202
        """Фабрика ленивого итератора загрузки — скрывает адаптеры от pipeline."""
        from adapters.confluence_loader import BatchPageLoader, PageLoader, SpaceLoader
        from domain.errors import ValidationError

        page_loader = PageLoader(ctx.cfg, ctx.confluence_loader_params)
        batch_loader = BatchPageLoader(page_loader)

        if ctx.space_key:
            if ctx.space_params is None:
                raise ValidationError("space_params обязателен при загрузке пространства")
            space_loader = SpaceLoader(batch_loader, ctx.cfg, ctx.space_params, ctx.confluence_loader_params)
            return space_loader.load(ctx.space_key)
        elif ctx.page_ids:
            return batch_loader.load(ctx.page_ids)
        else:
            raise ValidationError("Необходимо задать space_key или page_ids")

    from application.query_pipeline.factory import create_default_query_pipeline, create_search_pipeline
    from application.load_pipeline.factory import create_default_load_pipeline

    return AppServices(
        cfg=cfg,
        openai_client=openai_client,
        vectorstore_service=vs,
        embeddings=embeddings,
        query_pipeline=create_default_query_pipeline(),
        search_pipeline=create_search_pipeline(),
        load_pipeline=create_default_load_pipeline(),
        create_vectorstore=_create_vectorstore,
        document_preparer=DocumentPreparer(),
        create_confluence_importer=_create_confluence_importer,
        create_loading_events=_create_loading_events,
    )


# ---------------------------------------------------------------------------
# Фабрики инфраструктурных клиентов
# ---------------------------------------------------------------------------

LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def configure_logging(cfg: AppConfig) -> None:
    """Настроить логирование приложения в stdout."""
    level = LOG_LEVELS.get(cfg.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    logging.getLogger("watchdog").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def configure_ssl() -> None:
    """Отключить проверку SSL глобально (корпоративная среда)."""
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    try:
        ssl._create_default_https_context = ssl._create_unverified_context  # type: ignore[assignment]
    except AttributeError:
        pass


def create_openai_client(cfg: AppConfig) -> OpenAI:
    """Создать OpenAI-клиент из AppConfig."""
    http_client = httpx.Client(
        verify=cfg.ssl_verify,
        timeout=float(cfg.llm_timeout),
        headers={
            **cfg.litellm_auth_headers,
            "Content-Type": "application/json",
        },
    )
    return OpenAI(
        base_url=cfg.openai_url,
        api_key=cfg.litellm_api_key,
        http_client=http_client,
    )


def create_embeddings(cfg: AppConfig) -> LiteLLMEmbeddings:
    """Создать клиент эмбеддингов из AppConfig."""
    return LiteLLMEmbeddings(
        model=cfg.embedding_model,
        base_url=cfg.litellm_base_url,
        api_key=cfg.litellm_api_key,
        timeout=cfg.embedding_timeout,
        ssl_verify=cfg.ssl_verify,
        auth_headers=cfg.litellm_auth_headers,
    )
