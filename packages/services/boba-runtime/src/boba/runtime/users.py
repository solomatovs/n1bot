"""Пользователи чата на общем PostgreSQL-хранилище идентификации.

Ошибки:
DataUnavailableError — хранилище пользователей недоступно.
ExternalServiceError — прежний логин не сопоставлен новой личности.
AuthenticationError — у сохранённой строки нет подтверждённой личности.
ValueError — план миграции конфликтует с данными.
"""

from boba.chat.threads import DataUnavailableError
from boba.identity_postgres import UsersTable as IdentityUsers

__all__ = ["UsersTable"]


class UsersTable(IdentityUsers):
    """Адаптер общих users к ошибкам слоя данных чата для runtime и AID."""

    def _failure(self, action: str, exc: Exception) -> Exception:
        return DataUnavailableError(action, self._detail(action, exc))
