"""Kerberos-креды вызова: профиль соединения с билетом к его SPN вместо секции.

Keytab и пароль строки остаются у приложения, делегированный билет входа
лежит запечатанным в JWT сессии; в песочницу с профилем уезжает один
сервисный билет, выпущенный перед этим вызовом. Билет входа на исходе —
браузеру уходит сигнал обменяться заново.

Ошибки:
RefusalError — делегирования у субъекта нет, билет входа чужой или не
    открывается; kind ConnectionRefusal.NO_DELEGATION.
KerberosError — билет к соединению не выпущен, вызов начинать нечем.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from boba.connections.credentials import CredentialSource, ProfileSections
from boba.connections.http import HttpProfile
from boba.connections.marks import ConnectionRefusal
from boba.connections.profile import ConnectionProfile
from boba.identity.context import Credential, DelegatedTicket
from boba.identity.errors import RefusalError
from boba.identity.sso import RefreshSignal
from boba.kerberos import (
    DelegatedAuth,
    KerberosAuthBase,
    KerberosError,
    KerberosPasswordAuth,
    KeytabAuth,
    SignInTicket,
    TicketAuth,
    TicketSealError,
)
from boba.krb import (
    KerberosCredentials,
    KeytabCredentials,
    PasswordCredentials,
    ServiceTicketIssuer,
)
from boba.krb.seal import SsoTickets

__all__ = ["KerberosCredentialSource", "NoRefresh"]

logger = logging.getLogger(__name__)


class NoRefresh(RefreshSignal):
    """Процесс без сокета сессии: просить браузер обновить билет некому."""

    async def send(self) -> bool:
        return False


class KerberosCredentialSource(CredentialSource):
    """Билет вызова из keytab/пароля строки либо из делегированных кредов входа.

    Билет входа лежит запечатанным в JWT: строка users общая для всех способов
    входа, а JWT подписан приложением и описывает ровно этот вход. Процесс
    ничего не хранит — любой процесс с тем же секретом откроет билет.
    """

    REFRESH_BELOW: ClassVar[int] = 300
    """Остаток билета входа (сек), ниже которого просим браузер обменяться заново."""

    RETRY_HINT: ClassVar[str] = "retrying will not help until you sign in again"
    """Хвост отказа: агенту незачем повторять вызов, дело в самом входе."""

    def __init__(self, tickets: SsoTickets | None, refresh: RefreshSignal) -> None:
        self._tickets = tickets
        self._refresh = refresh

    async def for_connection(
        self, profile: ConnectionProfile, credential: Credential
    ) -> ConnectionProfile:
        section = ProfileSections.section_of(profile)
        if section is None:
            return profile

        if isinstance(section, TicketAuth):
            return profile

        source = await self._source(section, credential)
        issuer = ServiceTicketIssuer(section.min_lifetime)
        service = profile.service_name()
        ticket = await issuer.issue_async(source, service)

        logger.info(
            "kerberos: call ticket for %s -> %s (source: %s)",
            ticket.principal,
            service,
            section.trace(),
        )

        if isinstance(profile, HttpProfile):
            auth = profile.auth.model_copy(update={"kerberos": ticket})
            return profile.model_copy(update={"auth": auth})

        return profile.model_copy(update={"auth": ticket})

    async def _source(
        self, section: KerberosAuthBase, credential: Credential
    ) -> KerberosCredentials:
        """Креды, которыми выпускается билет вызова; билет источником не бывает."""
        if isinstance(section, DelegatedAuth):
            return await self._delegated(credential)

        if isinstance(section, KeytabAuth):
            return KeytabCredentials.of(section)

        if isinstance(section, KerberosPasswordAuth):
            return PasswordCredentials.of(section)

        msg = f"{type(section).__name__}: not a source of a call ticket"
        raise KerberosError(msg)

    async def _delegated(self, credential: Credential) -> KerberosCredentials:
        """Креды входа субъекта; билет на исходе — браузеру уходит сигнал."""
        sso = self._ticket_of(credential)
        if self._tickets is None:
            msg = (
                "this connection acts on your behalf, but Kerberos SSO is not "
                "configured in this deployment: ask the administrator for a "
                "connection with its own credentials"
            )
            raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg)

        ticket = self._opened(self._tickets, sso)
        if ticket.principal != sso.principal:
            msg = (
                f"the delegated ticket belongs to {ticket.principal} while "
                f"this session is {sso.principal}: sign out and sign in again; "
                f"{self.RETRY_HINT}"
            )
            raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg)

        await self._ensure_fresh(ticket)

        logger.info(
            "kerberos: acting as %s [ticket %ds]", ticket.principal, ticket.lifetime()
        )

        return self._tickets.credentials_of(ticket)

    async def _ensure_fresh(self, ticket: SignInTicket) -> None:
        """Просит браузер обновить билет входа, когда тот на исходе.

        Обмен идёт молча и кладёт в сессию новый JWT; ждать его вызов не
        обязан — пока билет жив, работает текущий.
        """
        if ticket.lifetime() >= self.REFRESH_BELOW:
            return

        logger.info(
            "kerberos: sign-in ticket of %s has %ds left, asking the browser",
            ticket.principal,
            ticket.lifetime(),
        )
        if not await self._refresh.send():
            logger.info("kerberos: nobody is listening for the refresh signal")

    @classmethod
    def _ticket_of(cls, credential: Credential) -> DelegatedTicket:
        """Билет из секретов субъекта; без него — NO_DELEGATION с причиной."""
        if isinstance(credential, DelegatedTicket):
            return credential

        logger.warning(
            "kerberos: a delegated ticket was asked without one: %s",
            credential.reason,
        )
        msg = f"{credential.reason}; {cls.RETRY_HINT}"
        raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg)

    @classmethod
    def _opened(cls, tickets: SsoTickets, sso: DelegatedTicket) -> SignInTicket:
        try:
            return tickets.open(sso.sealed)
        except TicketSealError as exc:
            msg = (
                f"the delegated Kerberos ticket in the session of {sso.principal} "
                "does not open (the application secret changed?): sign in again; "
                f"{cls.RETRY_HINT}"
            )
            raise RefusalError(ConnectionRefusal.NO_DELEGATION, msg) from exc
