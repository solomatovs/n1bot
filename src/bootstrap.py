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

import httpx
import urllib3
from openai import OpenAI

from embeddings import LiteLLMEmbeddings
from pipeline import Pipeline
from ui.state import AppConfig
from vectorstore import VectorStoreService


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


def bootstrap(cfg: AppConfig) -> AppServices:
    """Собрать все сервисы и пайплайны из конфигурации."""
    configure_logging(cfg)
    configure_ssl()

    embeddings = create_embeddings(cfg)
    openai_client = create_openai_client(cfg)
    vs = VectorStoreService(db_path=cfg.chroma_db_path, embedding=embeddings)

    from query_pipeline.factory import create_default_query_pipeline, create_search_pipeline
    from load_pipeline.factory import create_default_load_pipeline

    return AppServices(
        cfg=cfg,
        openai_client=openai_client,
        vectorstore_service=vs,
        embeddings=embeddings,
        query_pipeline=create_default_query_pipeline(),
        search_pipeline=create_search_pipeline(),
        load_pipeline=create_default_load_pipeline(),
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
        verify=False,
        timeout=float(cfg.llm_timeout),
        headers={
            "Authorization": f"Bearer {cfg.litellm_api_key}",
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
    )
