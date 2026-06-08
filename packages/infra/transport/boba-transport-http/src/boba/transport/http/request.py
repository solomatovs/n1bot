"""HttpRequest — чистый план одного HTTP-запроса (url + method)."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["HttpRequest"]


@dataclass(frozen=True)
class HttpRequest:
    """План одного HTTP-запроса. База (base_url/auth/headers/params/timeout/retry)
    приходит из HttpProfile, на котором сконструирован HttpTransport.

    url — относительный (склеится с profile.base_url) или абсолютный.
    method — HTTP-метод запроса.
    """

    url: str
    method: str = "GET"
