"""Стадии doc-пайплайна."""
from application.doc_pipeline.stages.agent_loop import AgentLoopStage
from application.doc_pipeline.stages.history import HistoryStage
from application.doc_pipeline.stages.system_prompt import SystemPromptStage
from application.doc_pipeline.stages.user_query import UserQueryStage

__all__ = [
    "AgentLoopStage",
    "SystemPromptStage",
    "HistoryStage",
    "UserQueryStage",
]
