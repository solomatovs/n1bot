"""UI-проекция Chainlit; metadata не участвует в авторизации."""

import chainlit as cl

from boba.identity.api import StoredUser
from boba.identity.authentication import AuthenticatedPrincipal
from boba.identity.errors import AuthenticationError


class SessionUser(cl.PersistedUser):
    """Пользователь, созданный только после проверки JWT нашим auth callback.

    principal — атрибут адаптера, не поле dataclass Chainlit: он не попадает
    в to_dict/asdict и не восстанавливается из metadata клиента или БД.
    Credentials остаются в токене.
    """

    principal: AuthenticatedPrincipal

    def __init__(self, principal: AuthenticatedPrincipal, stored: StoredUser) -> None:
        if principal.id != stored.id or principal.identity != stored.identity:
            raise AuthenticationError(
                "Chainlit projection differs from the authenticated user"
            )
        super().__init__(
            id=str(stored.id),
            identifier=principal.identifier,
            display_name=principal.identity.login,
            createdAt=stored.created_at.isoformat(),
            metadata={},
        )
        self.principal = principal
