"""Пользователи чата на общем PostgreSQL-хранилище идентификации.

Ошибки:
DataUnavailableError — хранилище пользователей недоступно.
DataBrokenError — схема или строка нарушает контракт хранилища.
DataConflictError — конкурентное изменение или конфликт данных.
"""

from boba.chat.threads import DataBrokenError, DataConflictError, DataUnavailableError
from boba.identity_postgres import UsersTable as IdentityUsers

__all__ = ["UsersTable"]


class UsersTable(IdentityUsers):
    """Адаптер общих users к ошибкам слоя данных чата для runtime и AID."""

    def _unavailable(self, action: str, exc: Exception) -> Exception:
        return DataUnavailableError(action, self._detail(action, exc))

    def _broken(self, action: str, exc: Exception) -> Exception:
        return DataBrokenError(action, self._detail(action, exc))

    def _conflict(self, action: str, detail: str) -> Exception:
        return DataConflictError(action, detail)
