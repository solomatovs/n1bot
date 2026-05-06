"""TextSplitter: ctor-валидация + soft-break поведение + lazy-семантика."""

from __future__ import annotations

import inspect

import pytest

from boba.patterns import ConverterInputError
from boba.chunking.splitter import TextSplitter


def test_short_text_yields_single_chunk():
    s = TextSplitter(chunk_size=100, chunk_overlap=10)
    assert list(s.convert("hello world")) == ["hello world"]


def test_empty_text_yields_no_chunks():
    s = TextSplitter(chunk_size=100, chunk_overlap=10)
    assert list(s.convert("")) == []
    assert list(s.convert("    ")) == []


def test_long_text_split_into_multiple_chunks():
    s = TextSplitter(chunk_size=20, chunk_overlap=5)
    text = " ".join(["word"] * 50)
    chunks = list(s.convert(text))
    assert len(chunks) > 1
    assert all(len(c) <= 20 for c in chunks)


def test_soft_break_prefers_paragraph_over_word():
    s = TextSplitter(chunk_size=20, chunk_overlap=0)
    text = "abc def ghi\n\njkl mno pqr stu"
    first = next(iter(s.convert(text)))
    assert first == "abc def ghi"


def test_invalid_chunk_size_raises():
    with pytest.raises(ConverterInputError):
        TextSplitter(chunk_size=0, chunk_overlap=0)


def test_invalid_overlap_raises():
    with pytest.raises(ConverterInputError):
        TextSplitter(chunk_size=10, chunk_overlap=10)
    with pytest.raises(ConverterInputError):
        TextSplitter(chunk_size=10, chunk_overlap=-1)


def test_immutable_reusable():
    s = TextSplitter(chunk_size=100, chunk_overlap=10)
    assert list(s.convert("first call")) == ["first call"]
    assert list(s.convert("second call")) == ["second call"]


def test_convert_returns_lazy_iterator_not_list():
    """Lazy-инвариант: convert() возвращает generator (без накопления в памяти)."""
    s = TextSplitter(chunk_size=20, chunk_overlap=5)
    result = s.convert(" ".join(["word"] * 50))
    assert inspect.isgenerator(result)


def test_lazy_consumption_one_chunk_at_a_time():
    """Можно взять первый chunk не материализуя весь output."""
    s = TextSplitter(chunk_size=20, chunk_overlap=5)
    text = " ".join(["word"] * 50)
    it = iter(s.convert(text))
    first = next(it)
    assert isinstance(first, str)
    assert len(first) <= 20
