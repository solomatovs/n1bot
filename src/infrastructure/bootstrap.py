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
from adapters.litellm_embeddings import LiteLLMEmbeddings
from domain.config import AppConfig
from domain.confluence_import import ConfluenceImportService
from domain.loading import ConfluenceImportParams
from domain.chat_renderer import ChatRenderer
from domain.vectorstore import VectorStoreService


@dataclass
class AppServices:
    """Разделяемые сервисы приложения — создаются один раз при старте."""

    cfg: AppConfig
    openai_client: OpenAI
    vectorstore_service: VectorStoreService
    embeddings: LiteLLMEmbeddings
    create_vectorstore: Callable[[str], VectorStoreService]
    create_confluence_importer: Callable[[ConfluenceImportParams], ConfluenceImportService]
    create_chat_renderer: Callable[..., ChatRenderer] = None  # type: ignore[assignment]


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

    return AppServices(
        cfg=cfg,
        openai_client=openai_client,
        vectorstore_service=vs,
        embeddings=embeddings,
        create_vectorstore=_create_vectorstore,
        create_confluence_importer=_create_confluence_importer,
        create_chat_renderer=_create_chat_renderer(),
    )


# ---------------------------------------------------------------------------
# Фабрики инфраструктурных клиентов
# ---------------------------------------------------------------------------

def _create_chat_renderer() -> Callable[..., ChatRenderer]:
    """Фабрика рендерера чата."""

    def factory() -> ChatRenderer:
        from ui.renderers.simple import SimpleChatRenderer
        return SimpleChatRenderer()

    return factory


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
