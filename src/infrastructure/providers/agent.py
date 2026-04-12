"""AgentProvider + ToolsProvider — per-folder сервисы (Scope.REQUEST)."""
from __future__ import annotations

from typing import Sequence

from dishka import Provider, Scope, from_context, provide
from openai import OpenAI

from adapters.chromadb_vectorstore import ChromaVectorStoreService
from adapters.jsonl_chat import JsonlChatReader, JsonlChatWriter
from application.readers.registry import DocumentReaderRegistry
from domain.core.storage import ChatReader, ChatWriter
from adapters.litellm_embeddings import LiteLLMEmbeddings
from application.agent.agent_loop import AgentLoop
from application.agent.system_prompt import SystemPromptFiller
from application.agent.tool_executor import ToolCallExecutor
from application.agent.tools_loader import ToolsFiller
from application.agent.user_prompt import UserPromptFiller
from application.agent.agent_loop import ContextPipeline
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
from domain.core.tools import Tool, ToolRegistry
from domain.config import AppConfig
from domain.importing.confluence import ConfluenceImportFactory
from domain.di_types import CollectionName, EmbeddingModel, FolderContext
from domain.core.vectorstore import VectorStoreService
from domain.workspace import Workspace


class AgentProvider(Provider):
    """Per-folder сервисы: workspace, vectorstore, collection_name."""
    scope = Scope.REQUEST

    folder_ctx = from_context(provides=FolderContext, scope=Scope.REQUEST)

    @provide
    def workspace(self, cfg: AppConfig, ctx: FolderContext) -> Workspace:
        boba = cfg.boba_path(ctx.folder_path)
        boba.mkdir(exist_ok=True)
        return Workspace(
            folder_path=ctx.folder_path,
            boba_path=boba,
            manifest_path=cfg.index_manifest_path(ctx.folder_path),
            history_path=ctx.history_path,
        )

    @provide
    def vectorstore(self, cfg: AppConfig, emb: LiteLLMEmbeddings, ctx: FolderContext) -> VectorStoreService:
        chroma_path = str(cfg.chroma_path(ctx.folder_path))
        return ChromaVectorStoreService(db_path=chroma_path, default_embedding=emb, cfg=cfg)

    @provide
    def collection_name(self, cfg: AppConfig, ctx: FolderContext) -> CollectionName:
        return CollectionName(cfg.collection_name(ctx.folder_path.name))

    @provide
    def chat_writer(self, ctx: FolderContext) -> ChatWriter:
        if ctx.history_path is None:
            raise ValueError("history_path is required for ChatWriter")
        return JsonlChatWriter(ctx.history_path)

    @provide
    def chat_reader(self, ctx: FolderContext) -> ChatReader:
        if ctx.history_path is None:
            raise ValueError("history_path is required for ChatReader")
        return JsonlChatReader(ctx.history_path)


class ToolsProvider(Provider):
    """Per-folder tools и AgentLoop."""
    scope = Scope.REQUEST

    @provide
    def index_tool(self, ws: Workspace, vs: VectorStoreService, coll: CollectionName, emb: EmbeddingModel, rr: DocumentReaderRegistry) -> IndexDocumentsTool:
        return IndexDocumentsTool(ws, vs, coll, emb, rr)

    @provide
    def search_tool(self, ws: Workspace, vs: VectorStoreService, coll: CollectionName) -> SearchDocumentsTool:
        return SearchDocumentsTool(ws, vs, coll)

    @provide
    def read_file_tool(self, ws: Workspace) -> ReadFileTool:
        return ReadFileTool(ws)

    @provide
    def list_files_tool(self, ws: Workspace, rr: DocumentReaderRegistry) -> ListFilesTool:
        return ListFilesTool(ws, rr)

    @provide
    def get_chat_history_tool(self, reader: ChatReader) -> GetChatHistoryTool:
        return GetChatHistoryTool(reader)

    @provide
    def delete_file_tool(self, ws: Workspace) -> DeleteFileTool:
        return DeleteFileTool(ws)

    @provide
    def edit_file_tool(self, ws: Workspace) -> EditFileTool:
        return EditFileTool(ws)

    @provide
    def delete_collection_tool(self, ws: Workspace, vs: VectorStoreService, coll: CollectionName) -> DeleteCollectionTool:
        return DeleteCollectionTool(ws, vs, coll)

    @provide
    def get_collection_info_tool(self, ws: Workspace, vs: VectorStoreService, coll: CollectionName) -> GetCollectionInfoTool:
        return GetCollectionInfoTool(ws, vs, coll)

    @provide
    def import_pages_tool(self, ws: Workspace, factory: ConfluenceImportFactory) -> ImportConfluencePagesTool:
        return ImportConfluencePagesTool(ws, factory)

    @provide
    def import_space_tool(self, ws: Workspace, factory: ConfluenceImportFactory) -> ImportConfluenceSpaceTool:
        return ImportConfluenceSpaceTool(ws, factory)

    @provide
    def tools(
        self,
        t1: IndexDocumentsTool,
        t2: SearchDocumentsTool,
        t3: ReadFileTool,
        t4: ListFilesTool,
        t5: GetChatHistoryTool,
        t6: DeleteFileTool,
        t7: EditFileTool,
        t8: DeleteCollectionTool,
        t9: GetCollectionInfoTool,
        t10: ImportConfluencePagesTool,
        t11: ImportConfluenceSpaceTool,
    ) -> Sequence[Tool]:
        return [t1, t2, t3, t4, t5, t6, t7, t8, t9, t10, t11]

    @provide
    def registry(self, tools: Sequence[Tool]) -> ToolRegistry:
        return ToolRegistry(tools)

    @provide
    def context_pipeline(self, ws: Workspace, registry: ToolRegistry) -> ContextPipeline:
        return ContextPipeline([
            SystemPromptFiller(ws),
            UserPromptFiller(),
            ToolsFiller(registry),
        ])

    @provide
    def tool_executor(self, registry: ToolRegistry) -> ToolCallExecutor:
        return ToolCallExecutor(registry)

    @provide
    def agent_loop(
        self,
        client: OpenAI,
        pipeline: ContextPipeline,
        executor: ToolCallExecutor,
    ) -> AgentLoop:
        return AgentLoop(
            openai_client=client,
            context_pipeline=pipeline,
            tool_executor=executor,
        )
