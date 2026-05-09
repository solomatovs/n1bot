"""
Transport - абстрактный интерфейс для получения сырых данных (RawDocument) по Request

Идея в том, что бы абстрагировать действия,
направленные на получение RawDocument'а
который содержит файловый описатель на нужные данные.

Transport может иметь внутреннее состояние
(например, пул HTTP-соединений, открытые файловые дескрипторы и т.п.).

К примеру:
- `Transport[HttpRequest]` может выполнить HTTP-запрос
    и вернуть файловый описатель на body ответа

 - `Transport[FsRequest]` может открыть файл и вернуть файловый описатель
"""

from __future__ import annotations

from typing import TypeVar

from boba.indexing.context import PipelineContext
from boba.indexing.raw_document import RawDocument
from boba.indexing.request import Request
from boba.patterns import StreamTransformer

__all__ = ["Transport"]

ReqT = TypeVar("ReqT", bound=Request)


class Transport(
    StreamTransformer[PipelineContext, ReqT, RawDocument],
):
    """Generic I/O для Request[ReqT] → RawDocument."""
