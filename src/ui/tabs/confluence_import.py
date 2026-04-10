"""Вкладка импорта — скачивание страниц из Confluence на диск (export_view HTML)."""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

import streamlit as st

from domain.confluence_import import (
    ImportDone,
    ImportEvent,
    ImportPageFailed,
    ImportPageSaved,
    ImportSpaceEnumerated,
)
from domain.loading import ConfluenceImportParams, SpaceLoadParams
from infrastructure.bootstrap import AppServices
from ui.components.folder_selector import folder_selector
from ui.state import SessionState


def render(services: AppServices, state: SessionState) -> None:
    st.title("Загрузка из Confluence")

    tab_space, tab_pages = st.tabs(["По Space Key", "По Page IDs"])

    with tab_space:
        _render_space_tab(services)

    with tab_pages:
        _render_page_tab(services)


# ---------------------------------------------------------------------------
# По Page IDs
# ---------------------------------------------------------------------------

def _render_page_tab(services: AppServices) -> None:
    import_params = _render_import_params(key_prefix="imp_pid")
    base_dir = Path(services.cfg.import_base_dir)
    output_dir = folder_selector(base_dir, key_prefix="imp_pid")

    st.subheader("Импорт страниц")
    pids_input = st.text_input("Page IDs через запятую", key="imp_pIds")

    if not st.button("Загрузить", key="btn_import_pages"):
        return

    page_ids = [x.strip() for x in pids_input.split(",") if x.strip()]
    if not page_ids:
        st.warning("Укажите хотя бы один Page ID.")
        return
    if output_dir is None:
        st.warning("Выберите папку.")
        return

    importer = services.create_confluence_importer(import_params)
    try:
        _consume_events(importer.import_pages(page_ids, output_dir))
    except Exception as e:
        st.error(f"Ошибка: {e}")
    finally:
        importer.close()


# ---------------------------------------------------------------------------
# По Space Key
# ---------------------------------------------------------------------------

def _render_space_tab(services: AppServices) -> None:
    import_params = _render_import_params(key_prefix="imp_sp")
    space_params = _render_space_params(key_prefix="imp_sp")
    base_dir = Path(services.cfg.import_base_dir)
    output_dir = folder_selector(base_dir, key_prefix="imp_sp")

    st.subheader("Импорт пространства")
    space_key = st.text_input("Space Key", key="imp_spaceKey")

    if not st.button("Загрузить пространство", key="btn_import_space"):
        return

    if not space_key:
        st.warning("Укажите Space Key.")
        return
    if output_dir is None:
        st.warning("Выберите папку.")
        return

    importer = services.create_confluence_importer(import_params)
    try:
        _consume_events(
            importer.import_space(space_key, space_params, output_dir),
        )
    except Exception as e:
        st.error(f"Ошибка: {e}")
    finally:
        importer.close()


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

def _render_import_params(key_prefix: str) -> ConfluenceImportParams:
    """Настройки подключения для импорта."""
    defaults = ConfluenceImportParams()
    p = key_prefix

    with st.expander("Настройки подключения", expanded=False):
        use_custom_token = st.checkbox(
            "Использовать свой токен", value=False, key=f"{p}_use_token",
        )
        token_input = st.text_input(
            "Confluence Token",
            type="password",
            disabled=not use_custom_token,
            help="Свой Personal Access Token для Confluence",
            key=f"{p}_token",
        )
        col1, col2 = st.columns(2)
        with col1:
            timeout = st.slider(
                "Таймаут (сек)", min_value=5, max_value=120,
                value=defaults.timeout, step=5, key=f"{p}_timeout",
            )
        with col2:
            ssl_verify = st.checkbox(
                "Проверять SSL", value=defaults.ssl_verify, key=f"{p}_ssl",
            )

    token = token_input.strip() if use_custom_token and token_input else None
    return ConfluenceImportParams(timeout=timeout, ssl_verify=ssl_verify, token=token)


def _render_space_params(key_prefix: str) -> SpaceLoadParams:
    """Настройки пространства."""
    defaults = SpaceLoadParams()
    p = key_prefix

    with st.expander("Настройки пространства", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            api_page_limit = st.slider(
                "Страниц на запрос API", min_value=1, max_value=200,
                value=defaults.api_page_limit, step=10,
                help="Размер страницы при пагинации Confluence REST API",
                key=f"{p}_api_page_limit",
            )
        with col2:
            use_limit = st.checkbox(
                "Ограничить количество страниц",
                value=defaults.max_pages is not None,
                key=f"{p}_use_max_pages",
            )
            max_pages = None
            if use_limit:
                max_pages = st.number_input(
                    "Макс. страниц", min_value=1, value=100,
                    key=f"{p}_max_pages",
                )

    return SpaceLoadParams(api_page_limit=api_page_limit, max_pages=max_pages)


# ---------------------------------------------------------------------------
# Обработчик событий
# ---------------------------------------------------------------------------

def _consume_events(events: Iterator[ImportEvent]) -> None:
    with st.status("Загрузка...", expanded=True) as status:
        pbar = st.progress(0.0)
        msg = st.empty()

        for event in events:
            match event:
                case ImportSpaceEnumerated(space_key=sk, total=n):
                    msg.write(f"Пространство «{sk}»: найдено {n} страниц")

                case ImportPageSaved(page_id=pid, title=t, index=i, total=total):
                    pbar.progress(i / total, text=f"{i}/{total} — {t} ({pid})")

                case ImportPageFailed(page_id=pid, error=err, index=i, total=total):
                    pbar.progress(i / total, text=f"{i}/{total} — ошибка: {pid}")
                    st.warning(f"Ошибка {pid}: {err}")

                case ImportDone(ok_count=ok, failed_count=bad, output_dir=out):
                    pbar.empty()
                    status.update(label="Готово", state="complete")
                    msg.write(f"Загрузка завершена. Сохранено: {ok}, ошибок: {bad}. Папка: {out}")
