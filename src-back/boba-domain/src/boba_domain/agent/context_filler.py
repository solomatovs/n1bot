"""ContextRequest — контекст для pipeline наполнения context window.

Fillers реализуют PipelineStage[ContextRequest, DocPipelineEvent].
Pipeline прогоняет их по цепочке перед первым вызовом LLM.
"""

from __future__ import annotations

from dataclasses import dataclass

from boba_domain.agent.context_window import ContextWindow


@dataclass
class ContextRequest:
    """Контекст для pipeline наполнения context window."""

    window: ContextWindow
    query: str
