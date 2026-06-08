"""HttpRequest — чистый план одного HTTP-запроса (url + method + headers/params + body)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["HttpRequest"]


@dataclass(frozen=True)
class HttpRequest:
    """План одного HTTP-запроса. Соединение (base_url/auth/timeout/retry/ssl)
    приходит из HttpProfile, на котором сконструирован HttpTransport.

    url — относительный (склеится с profile.base_url) или абсолютный.
    method — HTTP-метод запроса.
    headers/params — заголовки и query-параметры этого запроса (профиль их
    не задаёт — они целиком per-request).

    Body — взаимоисключающие способы задать тело (передавай максимум один,
    кроме легитимной пары data+files для multipart-формы); прозрачно уходят
    в httpx.Client.build_request:
    - content — сырое тело: bytes/str ИЛИ Iterable[bytes] для стриминга
      (генератор чанков -> upload без загрузки в память, chunked).
    - data    — формовые поля -> application/x-www-form-urlencoded.
    - files   — multipart/form-data; значение-файл может быть открытым
      бинарным файлом -> httpx стримит его с диска (не читает целиком).
    - json    — JSON-сериализуемый объект -> application/json.

    ВНИМАНИЕ retry: build_request зовётся заново на каждой попытке, поэтому
    реплеятся bytes/str/json/data (in-memory) И seekable-файлы в files —
    httpx делает seek(0) перед чтением, повтор шлёт файл с начала. Одноразовы
    только неперематываемые источники: генератор в content и non-seekable
    файло-подобные — для них используй профиль с retry_attempts=1.
    """

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    content: bytes | str | Iterable[bytes] | None = None
    data: Mapping[str, Any] | None = None
    files: Any | None = None
    json: Any | None = None
