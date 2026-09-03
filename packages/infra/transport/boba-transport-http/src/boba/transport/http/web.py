"""Привязка web-профиля к хосту запроса: проверка покрытия делается в теле.

Профиль соединения приходит инструменту параметром вызова; хост URL, который
инструмент собирается открыть, обязан попадать под base_url профиля — точный
или шаблон `*.domain`. Проверку делает сам инструмент: только он знает, какой
именно URL запрашивает.

Ошибки:
UnknownHostError — хост URL не покрыт соединением; текст готов для пользователя.
"""

from __future__ import annotations

from boba.transport.http.profile import HostPattern, HttpConnection

__all__ = ["UnknownHostError", "WebHost"]


class UnknownHostError(Exception):
    """Хост URL не покрыт выбранным соединением; текст готов для пользователя."""


class WebHost:
    """Профиль, привязанный к конкретному хосту запроса."""

    @staticmethod
    def bound(profile: HttpConnection, url: str) -> HttpConnection:
        """Профиль под этот URL; чужой хост — отказ.

        Ошибки:
        UnknownHostError — хост URL вне покрытия профиля.
        """
        host = HostPattern.host_of(url)

        if not profile.covers(host):
            msg = (
                f"web: host {host!r} is outside the chosen connection "
                f"(it covers {profile.host()!r}). URL={url!r}"
            )
            raise UnknownHostError(msg)

        return profile.bound_to(host)
