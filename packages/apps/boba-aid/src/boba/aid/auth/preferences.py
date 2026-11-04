"""Подсказка выбора профиля интерфейсу, отдельно от токена и прав сессии."""

from typing import ClassVar
from urllib.parse import quote

from starlette.responses import Response

from boba.aid.auth.cookies import CookieSpec


class SessionPreferences:
    """Cookie содержит только имя профиля. UI сверяет его с выданным набором."""

    PROFILE: ClassVar[str] = "boba_chat_profile"

    def __init__(self, cookie: CookieSpec) -> None:
        self._cookie = cookie

    def put(self, response: Response, preferred_profile: str) -> None:
        if not preferred_profile:
            response.delete_cookie(
                self.PROFILE,
                path=self._cookie.path,
                secure=self._cookie.secure,
                samesite=self._cookie.samesite,
            )
            return
        response.set_cookie(
            self.PROFILE,
            quote(preferred_profile, safe=""),
            path=self._cookie.path,
            secure=self._cookie.secure,
            samesite=self._cookie.samesite,
            max_age=self._cookie.ttl_sec,
            httponly=False,
        )
