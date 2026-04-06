"""Профилирование потокового чат-пайплайна (RAG → LLM → streaming).

Проверяет что пайплайн работает в потоковом режиме:
- события yield-ятся по одному (RetrievalStarted → RetrievalDone → ThinkingToken* → AnswerToken* → GenerationDone)
- нет аккумуляции в памяти — память стабильна между токенами
- порядок событий корректен

Запуск:
    python tests/test_chat_streaming.py
    python tests/test_chat_streaming.py --collection PAAS --query "что такое PAAS?" --model qwen3-0.6b
"""
# ruff: noqa: E402
from __future__ import annotations

import argparse
import sys
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from env_loader import setup

setup()

from query_pipeline.events import (
    AnswerToken,
    GenerationDone,
    RetrievalDone,
    RetrievalStarted,
    ThinkingToken,
)
from rag import run_chat_pipeline
from ui.state import AppConfig, PromptParams, SearchParams


def _fmt_mem(kb: float) -> str:
    if kb > 1024:
        return f"{kb / 1024:.1f}MB"
    return f"{kb:.0f}KB"


def run(collection: str, query: str, model: str) -> None:
    cfg = AppConfig()
    params = SearchParams()
    prompts = PromptParams()

    tracemalloc.start()

    print(f"=== Чат-пайплайн: collection={collection}, model={model} ===")
    print(f"    Запрос: {query}")
    print()

    event_order: list[str] = []
    token_count = 0
    thinking_tokens = 0
    answer_tokens = 0
    mem_at_tokens: list[float] = []

    for event in run_chat_pipeline(
        collection_name=collection,
        query=query,
        model=model,
        params=params,
        prompts=prompts,
        cfg=cfg,
    ):
        current, peak = tracemalloc.get_traced_memory()
        cur_kb = current / 1024
        peak_kb = peak / 1024
        event_name = type(event).__name__

        match event:
            case RetrievalStarted(query=q, collection=c):
                event_order.append(event_name)
                print(f"[SEARCH]   Ищу в «{c}»: {q[:60]}... | mem={_fmt_mem(cur_kb)}")

            case RetrievalDone(documents_found=n, sources_block=sb):
                event_order.append(event_name)
                sources_count = sb.count("\n") + 1 if sb else 0
                print(f"[FOUND]    Документов: {n}, источников: {sources_count} | mem={_fmt_mem(cur_kb)}")

            case ThinkingToken():
                if thinking_tokens == 0:
                    event_order.append("ThinkingToken")
                    print(f"[THINK]    Начало размышления... | mem={_fmt_mem(cur_kb)}")
                thinking_tokens += 1
                token_count += 1
                mem_at_tokens.append(cur_kb)

            case AnswerToken():
                if answer_tokens == 0:
                    event_order.append("AnswerToken")
                    print(f"[ANSWER]   Начало ответа... | mem={_fmt_mem(cur_kb)}")
                answer_tokens += 1
                token_count += 1
                mem_at_tokens.append(cur_kb)
                if answer_tokens % 50 == 0:
                    print(f"           ... {answer_tokens} токенов ответа | mem={_fmt_mem(cur_kb)}")

            case GenerationDone():
                event_order.append(event_name)
                print(f"[DONE]     Генерация завершена | mem={_fmt_mem(cur_kb)} peak={_fmt_mem(peak_kb)}")

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print("\n=== Итог ===")
    print(f"Порядок событий: {' → '.join(event_order)}")
    print(f"Токенов thinking: {thinking_tokens}")
    print(f"Токенов answer:   {answer_tokens}")
    print(f"Токенов всего:    {token_count}")
    print(f"Финальная память: {_fmt_mem(current / 1024)}")
    print(f"Пиковая память:   {_fmt_mem(peak / 1024)}")

    expected_start = ["RetrievalStarted", "RetrievalDone"]
    if event_order[:2] == expected_start:
        print("PASS: порядок событий корректен (поиск → генерация)")
    else:
        print(f"FAIL: ожидался {expected_start}, получен {event_order[:2]}")

    if event_order[-1] == "GenerationDone":
        print("PASS: генерация завершена корректно")
    else:
        print(f"FAIL: последнее событие — {event_order[-1]}, ожидалось GenerationDone")

    if len(mem_at_tokens) >= 10:
        first_quarter = mem_at_tokens[: len(mem_at_tokens) // 4]
        last_quarter = mem_at_tokens[3 * len(mem_at_tokens) // 4 :]
        avg_first = sum(first_quarter) / len(first_quarter)
        avg_last = sum(last_quarter) / len(last_quarter)
        growth = (avg_last - avg_first) / avg_first * 100 if avg_first > 0 else 0
        print(f"Рост памяти между 1-й и 4-й четвертью токенов: {growth:+.1f}%")
        if growth < 5:
            print("PASS: память стабильна при стриминге — нет аккумуляции в генераторе")
        else:
            print(f"WARN: память выросла на {growth:.1f}% — возможна аккумуляция")
    else:
        print(f"SKIP: слишком мало токенов ({len(mem_at_tokens)}) для анализа памяти")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Профилирование чат-пайплайна")
    parser.add_argument("--collection", default="PAAS", help="Имя коллекции в ChromaDB")
    parser.add_argument("--query", default="что такое PAAS?", help="Вопрос для чат-бота")
    parser.add_argument("--model", default="qwen3-0.6b", help="Модель LLM")
    args = parser.parse_args()

    run(
        collection=args.collection,
        query=args.query,
        model=args.model,
    )
