"""Профилирование потокового пайплайна загрузки из Confluence.

Проверяет что пайплайн работает в потоковом режиме:
- события чередуются (загрузка → чанкинг → сохранение)
- память не растёт линейно с количеством чанков
- батчи сохраняются по мере наполнения

Запуск:
    python tests/test_streaming_pipeline.py
    python tests/test_streaming_pipeline.py --space PAAS --max-pages 3 --batch-size 8
"""
from __future__ import annotations

import argparse
import os
import sys
import tracemalloc
from pathlib import Path

sys.path.insert(0, "src")

# ruff: noqa: E402
import warnings

warnings.filterwarnings("ignore")

# Загрузить secrets из .streamlit/secrets.toml в env (для запуска без Streamlit)
_secrets_path = Path(__file__).resolve().parent.parent / "docker" / ".streamlit" / "secrets.toml"
if _secrets_path.exists():
    for line in _secrets_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"')
        os.environ.setdefault(key, val)

from events import (
    ChunkingDone,
    ChunkProduced,
    LoadingDone,
    PageFailed,
    PageLoaded,
    SectionChunked,
    SpaceEnumerated,
    StorageDone,
    StoreBatchDone,
    StoreBatchFailed,
)
from loaders import run_space_pipeline
from ui.state import AppConfig, ChunkingParams, SpaceLoadParams, StorageParams
from vectorstore import VectorStoreService


def _fmt_mem(kb: float) -> str:
    if kb > 1024:
        return f"{kb / 1024:.1f}MB"
    return f"{kb:.0f}KB"


def run(space_key: str, max_pages: int, batch_size: int, collection: str) -> None:
    cfg = AppConfig()
    tracemalloc.start()

    print(f"=== Потоковый пайплайн: space={space_key}, max_pages={max_pages}, batch_size={batch_size} ===\n")

    mem_after_load: list[float] = []
    mem_after_store: list[float] = []

    for event in run_space_pipeline(
        space_key=space_key,
        collection_name=collection,
        cfg=cfg,
        space_params=SpaceLoadParams(api_page_limit=50, max_pages=max_pages),
        chunking_params=ChunkingParams(),
        storage_params=StorageParams(batch_size=batch_size),
    ):
        current, peak = tracemalloc.get_traced_memory()
        cur_kb = current / 1024
        peak_kb = peak / 1024

        match event:
            case SpaceEnumerated(total=n):
                print(f"[SPACE]    Найдено {n} страниц | mem={_fmt_mem(cur_kb)} peak={_fmt_mem(peak_kb)}")

            case PageLoaded(page_id=pid, index=i, total=t):
                mem_after_load.append(cur_kb)
                print(f"[LOAD]     {i}/{t} pageId={pid} | mem={_fmt_mem(cur_kb)} peak={_fmt_mem(peak_kb)}")

            case PageFailed(page_id=pid, index=i, total=t, error=err):
                print(f"[FAIL]     {i}/{t} pageId={pid}: {err} | mem={_fmt_mem(cur_kb)}")

            case LoadingDone(ok_count=ok, failed_count=bad):
                print(f"[LOAD OK]  ok={ok} bad={bad} | mem={_fmt_mem(cur_kb)} peak={_fmt_mem(peak_kb)}")

            case SectionChunked(doc_index=di, doc_total=dt, section_index=si, section_total=st_, section_title=title):
                print(f"[SECTION]  doc {di}/{dt}, секция {si}/{st_}: {title[:50]} | mem={_fmt_mem(cur_kb)}")

            case ChunkProduced(cumulative_chunks=cc):
                pass

            case ChunkingDone(total_chunks=n):
                print(f"[CHUNK OK] {n} чанков | mem={_fmt_mem(cur_kb)} peak={_fmt_mem(peak_kb)}")

            case StoreBatchDone(total_stored=s, total_chunks=tc):
                mem_after_store.append(cur_kb)
                print(f"[STORE]    сохранено {s}/{tc} | mem={_fmt_mem(cur_kb)} peak={_fmt_mem(peak_kb)}")

            case StoreBatchFailed(error=err):
                print(f"[STORE ERR] {err} | mem={_fmt_mem(cur_kb)}")

            case StorageDone(total_stored=s, total_failed=f):
                print(f"[DONE]     сохранено={s} ошибок={f} | mem={_fmt_mem(cur_kb)} peak={_fmt_mem(peak_kb)}")

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"\n=== Итог ===")
    print(f"Финальная память: {_fmt_mem(current / 1024)}")
    print(f"Пиковая память:   {_fmt_mem(peak / 1024)}")

    if len(mem_after_store) >= 2:
        first_half = mem_after_store[: len(mem_after_store) // 2]
        second_half = mem_after_store[len(mem_after_store) // 2 :]
        avg_first = sum(first_half) / len(first_half)
        avg_second = sum(second_half) / len(second_half)
        growth = (avg_second - avg_first) / avg_first * 100
        print(f"Рост памяти между первой и второй половиной батчей: {growth:+.1f}%")
        if growth < 10:
            print("PASS: память стабильна — потоковый подход работает")
        else:
            print("WARN: память растёт — возможна утечка или накопление")

    # cleanup
    print(f"\nУдаляю тестовую коллекцию «{collection}»...")
    VectorStoreService(cfg).remove_collection(collection)
    print("Готово.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Профилирование потокового пайплайна")
    parser.add_argument("--space", default="PAAS", help="Space Key в Confluence")
    parser.add_argument("--max-pages", type=int, default=3, help="Макс. страниц для загрузки")
    parser.add_argument("--batch-size", type=int, default=8, help="Размер батча для ChromaDB")
    parser.add_argument("--collection", default="test-streaming", help="Имя тестовой коллекции")
    args = parser.parse_args()

    run(
        space_key=args.space,
        max_pages=args.max_pages,
        batch_size=args.batch_size,
        collection=args.collection,
    )
