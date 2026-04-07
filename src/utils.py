"""Утилитные функции — именованные замены для inline-паттернов."""
from __future__ import annotations

import re
from typing import List, Sequence, TypeVar

import numpy as np

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Коллекции
# ---------------------------------------------------------------------------

def safe_index(items: Sequence[T], value: T, default: int = 0) -> int:
    """Найти индекс элемента в последовательности, или вернуть default."""
    try:
        return list(items).index(value)
    except ValueError:
        return default


def get_document_metadata(doc: object) -> dict:
    """Извлечь metadata из Document, гарантируя dict."""
    return dict(getattr(doc, "metadata", None) or {})


# ---------------------------------------------------------------------------
# Текст
# ---------------------------------------------------------------------------

def split_paragraphs(text: str) -> List[str]:
    """Разделить текст на непустые параграфы по двойному переносу строки."""
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def split_comma_list(text: str) -> List[str]:
    """Разделить строку по запятым, убрав пустые элементы."""
    return [x.strip() for x in text.split(",") if x.strip()]


def strip_list_markers(lines: str) -> List[str]:
    """Разобрать ответ LLM с маркерами списка в чистые строки."""
    return [s.strip("- ").strip() for s in lines.splitlines() if s.strip()]


def add_prefix_to_texts(texts: List[str], prefix: str) -> List[str]:
    """Добавить префикс к каждому тексту."""
    return [f"{prefix}{t}" for t in texts]


# ---------------------------------------------------------------------------
# Математика
# ---------------------------------------------------------------------------

def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """Вычислить косинусное сходство между двумя векторами."""
    a = np.asarray(vec_a)
    b = np.asarray(vec_b)
    norm_product = np.linalg.norm(a) * np.linalg.norm(b)
    if norm_product == 0:
        return 0.0
    return float(np.dot(a, b) / norm_product)


# ---------------------------------------------------------------------------
# Markdown-парсинг
# ---------------------------------------------------------------------------

HEADING_PATTERN = re.compile(r"^(#+)\s+(.+)$")
LIST_ITEM_PATTERN = re.compile(r"^[\d+\.\-\*\+]\s")
LIST_SPLIT_PATTERN = re.compile(r"\n(?=[\d+\.\-\*\+]\s)")


def is_heading(line: str) -> re.Match | None:
    """Проверить, является ли строка markdown-заголовком."""
    return HEADING_PATTERN.match(line)


def is_list_item(line: str) -> bool:
    """Проверить, является ли строка элементом markdown-списка."""
    return LIST_ITEM_PATTERN.match(line) is not None


def is_code_line(line: str) -> bool:
    """Проверить, является ли строка кодом (отступ или ```)."""
    return line.startswith("    ") or "```" in line


def is_table_line(line: str) -> bool:
    """Проверить, является ли строка таблицей (содержит |, 3+ столбца)."""
    return "|" in line and len(line.split("|")) > 2


def split_list_items(text: str) -> List[str]:
    """Разделить текст на элементы markdown-списка."""
    return LIST_SPLIT_PATTERN.split(text)


def count_matching_lines(lines: Sequence[str], predicate) -> int:
    """Подсчитать количество строк, удовлетворяющих предикату."""
    return sum(1 for line in lines if predicate(line))


def line_ratio(matching: int, total: int) -> float:
    """Вычислить долю совпавших строк."""
    return matching / max(1, total)


# ---------------------------------------------------------------------------
# Метаданные документов
# ---------------------------------------------------------------------------

_HEADING_KEYS = ("Header 1", "Header 2")


def extract_heading(metadata: dict) -> str:
    """Извлечь заголовок из метаданных документа по приоритету ключей."""
    for key in _HEADING_KEYS:
        value = metadata.get(key)
        if value:
            return str(value)
    return ""


def format_source_line(space: str, pid: str, heading: str, url: str) -> str:
    """Форматировать строку источника для отображения пользователю."""
    line = f"- {space}:{pid} {heading}".strip()
    if url:
        line += f" — {url}"
    return line


# ---------------------------------------------------------------------------
# JSON-ответы API
# ---------------------------------------------------------------------------

def extract_model_ids(response_json: dict) -> List[str]:
    """Извлечь и отсортировать ID моделей из ответа /v1/models."""
    data = response_json.get("data", [])
    model_ids = [m["id"] for m in data if "id" in m]
    return sorted(model_ids)


def extract_page_ids_from_api(response_json: dict) -> List[str]:
    """Извлечь ID страниц из ответа Confluence REST API."""
    results = response_json.get("results") or []
    return [str(item["id"]) for item in results if "id" in item]


def extract_embeddings(response_json: dict) -> List[List[float]]:
    """Извлечь векторы эмбеддингов из ответа /v1/embeddings."""
    return [item["embedding"] for item in response_json["data"]]
