"""Выбор пространства входа: единственное выбирается без участия пользователя.

Ошибки:
AuthorizationError — пространство неизвестно или выбор неоднозначен.
"""

from collections.abc import Collection

from boba.identity.errors import AuthorizationError


class RealmSelection:
    """Общее правило выбора пространства для парольного, SPNEGO и proxy-входа."""

    def selected(self, realm: str, known: Collection[str], action: str) -> str:
        if realm:
            if realm not in known:
                msg = f"{action}: realm {realm!r} is not configured"
                raise AuthorizationError(msg)

            return realm

        if len(known) == 1:
            return next(iter(known))

        msg = (
            f"{action}: select an identity realm; "
            f"the configured realms are {sorted(known)}"
        )
        raise AuthorizationError(msg)
