"""Bootstrap — центральная точка сборки приложения.

Создаёт все разделяемые сервисы один раз при старте.
Остальные модули получают готовые зависимости через AppServices.
"""
from __future__ import annotations

import logging
import ssl
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
import urllib3
from openai import OpenAI

from adapters.chromadb_vectorstore import ChromaVectorStoreService
from adapters.confluence_importer import ConfluenceImporter
from adapters.litellm_embeddings import LiteLLMEmbeddings
from domain.config import AppConfig
from domain.importing.confluence import ConfluenceImportService
from domain.importing.loading import ConfluenceImportParams
from domain.search.vectorstore import VectorStoreService
from domain.workspace import Workspace

from application.agent.agent_loop import AgentLoop
from application.agent.tools import (
    DeleteCollectionTool,
    DeleteFileTool,
    EditFileTool,
    GetChatHistoryTool,
    GetCollectionInfoTool,
    ImportConfluencePagesTool,
    ImportConfluenceSpaceTool,
    IndexDocumentsTool,
    ListFilesTool,
    ReadFileTool,
    SearchDocumentsTool,
)


# ---------------------------------------------------------------------------
# Типизированные фабрики (Protocol вместо Callable[...])
# ---------------------------------------------------------------------------

class AgentFactory(Protocol):
    def __call__(self, folder_path: Path, *, history_path: Path | None = None) -> AgentLoop: ...


class VectorStoreFactory(Protocol):
    def __call__(self, db_path: str) -> VectorStoreService: ...


class ConfluenceImporterFactory(Protocol):
    def __call__(self, params: ConfluenceImportParams) -> ConfluenceImportService: ...


# ---------------------------------------------------------------------------
# AppServices
# ---------------------------------------------------------------------------

@dataclass
class AppServices:
    """Разделяемые сервисы приложения.

    Содержит только то, что нужно потребителям (UI, application).
    Внутренние детали (openai_client, embeddings) — скрыты в bootstrap.
    """
    cfg: AppConfig
    create_agent: AgentFactory
    create_vectorstore: VectorStoreFactory
    create_confluence_importer: ConfluenceImporterFactory


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def bootstrap(cfg: AppConfig) -> AppServices:
    """Собрать все сервисы из конфигурации."""
    configure_logging(cfg)
    configure_ssl()

    embeddings = _create_embeddings(cfg)
    openai_client = _create_openai_client(cfg)

    def create_vectorstore(db_path: str) -> VectorStoreService:
        return ChromaVectorStoreService(db_path=db_path, default_embedding=embeddings, cfg=cfg)

    def create_confluence_importer(params: ConfluenceImportParams) -> ConfluenceImportService:
        return ConfluenceImporter(cfg, params)

    def create_agent(folder_path: Path, *, history_path: Path | None = None) -> AgentLoop:
        cfg.boba_path(folder_path).mkdir(exist_ok=True)
        ws = Workspace(
            folder_path=folder_path,
            manifest_path=cfg.index_manifest_path(folder_path),
            history_path=history_path,
        )
        vs = create_vectorstore(str(cfg.chroma_path(folder_path)))
        coll = cfg.collection_name(folder_path.name)
        return AgentLoop(
            openai_client=openai_client,
            workspace=ws,
            tools=[
                IndexDocumentsTool(ws, vs, coll, cfg.embedding_model),
                SearchDocumentsTool(ws, vs, coll),
                ReadFileTool(ws),
                ListFilesTool(ws),
                GetChatHistoryTool(ws),
                DeleteFileTool(ws),
                EditFileTool(ws),
                DeleteCollectionTool(ws, vs, coll),
                GetCollectionInfoTool(ws, vs, coll),
                ImportConfluencePagesTool(ws, cfg),
                ImportConfluenceSpaceTool(ws, cfg),
            ],
        )

    return AppServices(
        cfg=cfg,
        create_agent=create_agent,
        create_vectorstore=create_vectorstore,
        create_confluence_importer=create_confluence_importer,
    )


# ---------------------------------------------------------------------------
# Инфраструктура
# ---------------------------------------------------------------------------

_LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def configure_logging(cfg: AppConfig) -> None:
    """Настроить логирование приложения в stdout."""
    level = _LOG_LEVELS.get(cfg.log_level.upper(), logging.INFO)
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


def _create_openai_client(cfg: AppConfig) -> OpenAI:
    http_client = httpx.Client(
        verify=cfg.ssl_verify,
        timeout=float(cfg.llm_timeout),
        headers={**cfg.litellm_auth_headers, "Content-Type": "application/json"},
    )
    return OpenAI(base_url=cfg.openai_url, api_key=cfg.litellm_api_key, http_client=http_client)


def _create_embeddings(cfg: AppConfig) -> LiteLLMEmbeddings:
    return LiteLLMEmbeddings(
        model=cfg.embedding_model,
        base_url=cfg.litellm_base_url,
        api_key=cfg.litellm_api_key,
        timeout=cfg.embedding_timeout,
        ssl_verify=cfg.ssl_verify,
        auth_headers=cfg.litellm_auth_headers,
    )
