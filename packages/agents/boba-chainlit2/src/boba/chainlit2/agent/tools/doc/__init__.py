"""Инструменты чтения документов из workspace (liteparse)."""

from boba.chainlit2.agent.tools.doc.config import DocToolsConfig
from boba.chainlit2.agent.tools.doc.tools import build_doc_tools

__all__ = ["DocToolsConfig", "build_doc_tools"]
