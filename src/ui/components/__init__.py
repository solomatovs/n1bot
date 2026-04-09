"""UI-компоненты — публичный API."""
from ui.components.chat_history import render_chat_history
from ui.components.collections import (
    collection_preview,
    collection_to_dataframe,
    list_collection_names,
)
from ui.components.models import get_chat_models, get_embedding_models
from ui.components.selectors import (
    collection_selector,
    embedding_model_selector,
    model_selector,
)
from ui.components.settings import (
    SpaceSettings,
    render_chunking_settings,
    render_generation_settings,
    render_multiquery_settings,
    render_page_id_settings,
    render_prompt_settings,
    render_retrieval_settings,
    render_space_settings,
)

__all__ = [
    "collection_preview",
    "collection_selector",
    "collection_to_dataframe",
    "embedding_model_selector",
    "get_chat_models",
    "get_embedding_models",
    "list_collection_names",
    "model_selector",
    "render_chat_history",
    "render_chunking_settings",
    "render_generation_settings",
    "render_multiquery_settings",
    "render_page_id_settings",
    "render_prompt_settings",
    "render_retrieval_settings",
    "render_space_settings",
    "SpaceSettings",
]
