"""Вкладка «Векторное хранилище» — индексация и просмотр per-folder коллекций."""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import streamlit as st

from application.index_pipeline import (
    BatchStored,
    ChunkCreated,
    CollectionPrepared,
    FileCompleted,
    FileStarted,
    IndexEvent,
    IndexingDone,
    IndexingSkipped,
    ManifestChecked,
    create_index_context,
    run_indexing,
)
from domain.errors import VectorStoreError
from infrastructure.bootstrap import AppServices
from ui.components.collections import (
    collection_preview,
    collection_to_dataframe,
    list_collection_names,
)
from ui.state import SessionState


def render(services: AppServices, state: SessionState) -> None:
    st.title("Векторное хранилище")

    cfg = services.cfg
    base_dir = Path(cfg.import_base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    folders = sorted(
        d.name for d in base_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )
    if not folders:
        st.info("Нет папок с документами.")
        return

    selected = st.selectbox("Папка с документами", folders, key="vs_folder")
    if not selected:
        return

    folder_path = base_dir / selected
    chroma_path = str(cfg.chroma_path(folder_path))
    collection_name = cfg.collection_name(selected)
    vs = services.create_vectorstore(chroma_path)

    # Кнопка индексации — всегда доступна
    _render_index_button(folder_path, services)

    # Просмотр коллекции — если существует
    colls = list_collection_names(vs)
    if not colls or collection_name not in [c for c in colls]:
        st.info(f"В папке «{selected}» нет векторной базы. Нажмите «Индексировать».")
        return

    _render_collection(vs, collection_name)


# ---------------------------------------------------------------------------
# Индексация
# ---------------------------------------------------------------------------

def _render_index_button(folder_path: Path, services: AppServices) -> None:
    if not st.button("Индексировать", key="vs_reindex"):
        return

    ctx = create_index_context(folder_path, services)
    _consume_index_events(run_indexing(ctx))


def _consume_index_events(events: Iterator[IndexEvent]) -> None:
    with st.status("Индексация...", expanded=True) as status:
        pbar = st.progress(0.0)
        msg = st.empty()

        for event in events:
            match event:
                case ManifestChecked(files_changed=changed, file_count=n):
                    if changed:
                        msg.write(f"Обнаружены изменения ({n} файлов)...")
                    else:
                        msg.write("Проверка актуальности индекса...")

                case IndexingSkipped(collection=name, doc_count=n):
                    pbar.empty()
                    status.update(label="Индекс актуален", state="complete")
                    msg.write(f"Коллекция «{name}»: {n} чанков, изменений нет.")

                case CollectionPrepared(collection=name):
                    msg.write(f"Коллекция «{name}» подготовлена...")

                case FileStarted(filename=name, index=i, total=total):
                    pbar.progress(max(0.01, (i - 1) / total), text=f"{i}/{total} — {name}")

                case ChunkCreated():
                    pass

                case BatchStored(total_stored=n):
                    msg.write(f"Сохранено {n} чанков...")

                case FileCompleted(filename=name, chunks=c, index=i, total=total):
                    pbar.progress(i / total, text=f"{i}/{total} — {name} ({c} чанков)")

                case IndexingDone(total_files=f, total_chunks=c):
                    pbar.empty()
                    status.update(label="Индексация завершена", state="complete")
                    msg.write(f"Проиндексировано: {f} файлов, {c} чанков.")


# ---------------------------------------------------------------------------
# Просмотр коллекции
# ---------------------------------------------------------------------------

def _render_collection(vs, collection_name: str) -> None:
    st.divider()
    st.caption(f"Коллекция: **{collection_name}**")

    emb_model = vs.get_collection_embedding_model(collection_name)
    st.caption(f"Embedding модель: **{emb_model or 'не задана'}**")

    show_full = st.toggle("Показывать полный состав коллекции", value=False, key="vs_full")
    if show_full:
        with st.spinner("Гружу полный состав…"):
            _show_full_collection(vs, collection_name)
    else:
        st.caption("Превью (сокращено до 200 символов):")
        st.dataframe(
            collection_preview(vs, collection_name),
            height=400,
        )

    if st.button(f"Удалить коллекцию «{collection_name}»", type="primary", key="vs_delete"):
        try:
            vs.remove_collection(collection_name)
            st.success("Коллекция удалена.")
            st.rerun()
        except VectorStoreError as e:
            st.error(f"Не удалось удалить: {e}")


def _show_full_collection(vs, name: str) -> None:
    try:
        df = collection_to_dataframe(vs, name)
        st.success(f"Документов: {len(df)} в «{name}»")
        st.dataframe(df, height=500)
    except VectorStoreError as e:
        st.error(f"Ошибка при получении коллекции: {e}")
