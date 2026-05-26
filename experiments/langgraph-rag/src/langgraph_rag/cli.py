"""CLI: langgraph-rag ingest | ask '...'."""

import asyncio
import logging

import typer

from langgraph_rag.config import Settings
from langgraph_rag.debug import DebugLogging
from langgraph_rag.ingest import ConfluenceIngestPipeline
from langgraph_rag.llm_factory import ChatModelFactory
from langgraph_rag.pipeline import RagPipeline
from langgraph_rag.retriever import HybridRetrieverFactory

app = typer.Typer(help="LangGraph RAG-эксперимент.")


@app.command()
def ingest(
    recreate: bool = typer.Option(
        False,
        "--recreate",
        help="Пересоздать PGVector-коллекцию и BM25-корпус с нуля.",
    ),
) -> None:
    """Загружает страницы Confluence в PGVector и BM25-корпус."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.load()
    DebugLogging.apply(settings)
    pipeline = ConfluenceIngestPipeline(settings)
    pipeline.run(recreate=recreate)


@app.command()
def ask(query: str) -> None:
    """Запускает RAG-граф на одном запросе."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.load()
    DebugLogging.apply(settings)
    chat_models = ChatModelFactory(settings)
    retriever = HybridRetrieverFactory(settings).build()
    pipeline = RagPipeline(
        classifier=chat_models.for_classifier(),
        keyword_extractor=chat_models.for_keyword_extractor(),
        answerer=chat_models.for_answerer(),
        retriever=retriever,
    )
    graph = pipeline.build_graph()
    final_state = asyncio.run(graph.ainvoke({"query": query}))
    typer.echo(final_state.get("answer", "<нет ответа>"))


if __name__ == "__main__":
    app()
