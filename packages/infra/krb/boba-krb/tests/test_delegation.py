"""Делегирование входа на живом KDC стенда: forwarded и constrained.

Браузер моделируется initiate-контекстом второго пользователя стенда (TGT по
паролю из конфига); сервис — accept по keytab приложения. Constrained требует
на стенде выданного делегирования на целевой SPN и снятого флага sensitive
у принципала приложения.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import krb5
import pytest
from gssapi import Credentials, Name, NameType, RequirementFlag, SecurityContext

from boba.connections.kerberos import (
    AcceptConfig,
    ConstrainedDelegation,
    DelegationMode,
    ForwardedDelegation,
    KeytabAuth,
)
from boba.krb import (
    CcacheLifetime,
    DelegatedCredentials,
    KerberosEnv,
    KerberosWorkspace,
    KeytabCredentials,
    ServiceTicketIssuer,
    SpnegoAcceptor,
    TicketCapture,
    TicketCredentials,
)
from boba.krb.seal import TicketSealer
from boba.stand.site import Stand

STAND = Stand.required()
KRB5_CONF = Path(STAND.krb_config)
SERVICE_KEYTAB = Path(STAND.krb_http_keytab)
SERVICE_SPN = f"HTTP/{STAND.krb_domain}@{STAND.krb_realm}"
SERVICE_PRINCIPAL = STAND.service_principal
USER_PRINCIPAL = STAND.reader_principal
TARGET = STAND.pg_spn

live_kdc = pytest.mark.skipif(
    not STAND.live(),
    reason="нет keytab/krb5.conf стенда",
)

pytestmark = [live_kdc]


@pytest.fixture
def krb5_env() -> Iterator[None]:
    saved = os.environ.get(KerberosEnv.CONFIG)
    os.environ[KerberosEnv.CONFIG] = str(KRB5_CONF)
    yield
    if saved is None:
        os.environ.pop(KerberosEnv.CONFIG, None)
        return
    os.environ[KerberosEnv.CONFIG] = saved


def _user_password() -> str:
    return STAND.reader_password.get_secret_value()


class Browser:
    """Клиентская сторона SSO: TGT пользователя и AP-REQ к SPN сервиса."""

    @staticmethod
    def ticket(tmp_path: Path) -> bytes:
        context = krb5.init_context()
        user = krb5.parse_name_flags(context, USER_PRINCIPAL.encode())
        options = krb5.get_init_creds_opt_alloc(context)
        krb5.get_init_creds_opt_set_forwardable(options, True)
        tgt = krb5.get_init_creds_password(
            context, user, options, _user_password().encode()
        )
        ccache = f"FILE:{tmp_path / 'browser'}"
        cache = krb5.cc_resolve(context, ccache.encode())
        krb5.cc_initialize(context, cache, user)
        krb5.cc_store_cred(context, cache, tgt)

        creds = Credentials(usage="initiate", store={b"ccache": ccache.encode()})
        target = Name(SERVICE_SPN, NameType.kerberos_principal)
        # без флагов делегирования: TGT сервису не форвардится
        initiator = SecurityContext(name=target, creds=creds, usage="initiate", flags=0)
        return initiator.step()

    @staticmethod
    def delegating_ticket(tmp_path: Path) -> bytes:
        """AP-REQ с делегированием: так ведёт себя браузер, которому его включили.

        KDC помечает билет ok-as-delegate, и клиент форвардит сервису свой TGT
        вместо evidence-тикета — режим constrained такие креды не принимает.
        """
        context = krb5.init_context()
        user = krb5.parse_name_flags(context, USER_PRINCIPAL.encode())
        options = krb5.get_init_creds_opt_alloc(context)
        krb5.get_init_creds_opt_set_forwardable(options, True)
        tgt = krb5.get_init_creds_password(
            context, user, options, _user_password().encode()
        )
        ccache = f"FILE:{tmp_path / 'delegating-browser'}"
        cache = krb5.cc_resolve(context, ccache.encode())
        krb5.cc_initialize(context, cache, user)
        krb5.cc_store_cred(context, cache, tgt)

        creds = Credentials(usage="initiate", store={b"ccache": ccache.encode()})
        target = Name(SERVICE_SPN, NameType.kerberos_principal)
        initiator = SecurityContext(
            name=target,
            creds=creds,
            usage="initiate",
            flags=int(RequirementFlag.delegate_to_peer)
            | int(RequirementFlag.mutual_authentication),
        )
        return initiator.step()


def _accept() -> AcceptConfig:
    return AcceptConfig(service_name=SERVICE_SPN, keytab=str(SERVICE_KEYTAB))


def _constrained(tmp_path: Path) -> ConstrainedDelegation:
    return ConstrainedDelegation(
        service_ccache=f"FILE:{tmp_path / 'service'}",
        krb5_config=str(KRB5_CONF),
    )


def _forwarded() -> ForwardedDelegation:
    return ForwardedDelegation(
        krb5_config=str(KRB5_CONF),
    )


def _servers(ticket_ccache: str) -> list[tuple[str, str]]:
    context = krb5.init_context()
    cache = krb5.cc_resolve(context, ticket_ccache.encode())
    pairs: list[tuple[str, str]] = []
    for cred in cache:
        client = krb5.unparse_name_flags(context, cred.client).decode()
        server = krb5.unparse_name_flags(context, cred.server).decode()
        pairs.append((client, server))
    return pairs


class TestConstrained:
    def test_login_yields_a_postgres_ticket_for_the_user(
        self, tmp_path: Path, krb5_env: None
    ) -> None:
        delegation = _constrained(tmp_path)
        acceptor = SpnegoAcceptor(_accept(), delegation)
        identity = acceptor.accept(Browser.ticket(tmp_path))
        if identity.principal != USER_PRINCIPAL:
            raise AssertionError(f"accepted someone else: {identity.principal}")

        sign_in = TicketCapture(delegation).capture(identity)
        if sign_in is None:
            raise AssertionError("constrained login must capture evidence credentials")
        if sign_in.lifetime() <= 0:
            raise AssertionError("captured ticket must have a lifetime")

        # билет переживает «границу процесса»: запечатан и открыт другим sealer'ом
        reopened = TicketSealer("stand-secret").open(
            TicketSealer("stand-secret").seal(sign_in)
        )
        credentials = DelegatedCredentials(reopened, str(KRB5_CONF))
        with credentials.applied():
            if CcacheLifetime.tgt(credentials.ccache, USER_PRINCIPAL) != 0:
                raise AssertionError("constrained ccache must not hold the user's TGT")

        ticket = ServiceTicketIssuer(min_lifetime=60).issue(credentials, TARGET)
        if ticket.principal != USER_PRINCIPAL:
            raise AssertionError("ticket must be issued for the user")

        shipped = TicketCredentials(ticket)
        with shipped.applied():
            pairs = _servers(shipped.ccache)

        if len(pairs) != 1:
            raise AssertionError(f"ticket ccache must hold one credential: {pairs}")
        client, server = pairs[0]
        if client != USER_PRINCIPAL or not server.startswith("postgres/postgres-17"):
            raise AssertionError(f"unexpected ticket: {pairs}")

    def test_forwarded_tgt_is_rejected_in_constrained_mode(
        self, tmp_path: Path, krb5_env: None
    ) -> None:
        """Ccache с TGT пользователя не подходит режиму constrained."""
        KerberosWorkspace.configure(str(KRB5_CONF), str(tmp_path / "cache"))
        credentials = KeytabCredentials.of(
            KeytabAuth(
                method="kerberos_keytab",
                principal=SERVICE_PRINCIPAL,
                keytab=str(SERVICE_KEYTAB),
            )
        )
        credentials.ensure()

        reason = TicketCapture.mismatch(
            credentials.ccache, SERVICE_PRINCIPAL, DelegationMode.CONSTRAINED
        )
        if "forwarded TGT" not in reason:
            raise AssertionError(f"TGT must be rejected: {reason!r}")


class TestBrowserDelegationIsRefused:
    """Браузер с включённым делегированием шлёт TGT: constrained его не берёт.

    Так выглядит рабочая станция, которой когда-то включили делегирование под
    forwarded-режим: KDC ставит билету ok-as-delegate, клиент форвардит TGT, и
    evidence-тикета в кредах не оказывается вовсе.
    """

    def test_forwarded_tgt_arrives_instead_of_evidence(
        self, tmp_path: Path, krb5_env: None
    ) -> None:
        """Проверка сути: делегируя, клиент отдаёт именно TGT."""
        delegation = _constrained(tmp_path)
        acceptor = SpnegoAcceptor(_accept(), delegation)

        identity = acceptor.accept(Browser.delegating_ticket(tmp_path))
        if identity.delegated is None:
            raise AssertionError("делегирующий клиент обязан прислать креды")

        ccache = f"FILE:{tmp_path / 'delegated'}"
        identity.delegated.store(
            store={b"ccache": ccache.encode()}, usage="initiate", overwrite=True
        )

        servers = [server for _, server in _servers(ccache)]
        if not any(server.startswith("krbtgt/") for server in servers):
            raise AssertionError(f"ожидался форварднутый TGT: {servers}")

        evidence = [server for server in servers if not server.startswith("krbtgt/")]
        if evidence:
            raise AssertionError(f"evidence-тикета тут быть не может: {evidence}")

    def test_constrained_login_refuses_such_credentials(
        self, tmp_path: Path, krb5_env: None
    ) -> None:
        """Вход состоится, но делегирования у него нет: метка входа пустая."""
        delegation = _constrained(tmp_path)
        acceptor = SpnegoAcceptor(_accept(), delegation)
        identity = acceptor.accept(Browser.delegating_ticket(tmp_path))

        sign_in = TicketCapture(delegation).capture(identity)

        if sign_in is not None:
            raise AssertionError("forwarded TGT не должен становиться входом")

    def test_reason_names_the_forwarded_tgt(self, tmp_path: Path) -> None:
        """Причина отказа называет ровно то, что случилось: пришёл TGT."""
        reason = TicketCapture.mismatch(
            _user_ccache(tmp_path), USER_PRINCIPAL, DelegationMode.CONSTRAINED
        )

        if "forwarded TGT" not in reason:
            raise AssertionError(f"причина должна называть TGT: {reason!r}")


def _user_ccache(tmp_path: Path) -> str:
    """Ccache с одним лишь TGT пользователя: то же, что приносит делегирование."""
    context = krb5.init_context()
    user = krb5.parse_name_flags(context, USER_PRINCIPAL.encode())
    options = krb5.get_init_creds_opt_alloc(context)
    krb5.get_init_creds_opt_set_forwardable(options, True)
    tgt = krb5.get_init_creds_password(
        context, user, options, _user_password().encode()
    )

    ccache = f"FILE:{tmp_path / 'user-tgt'}"
    cache = krb5.cc_resolve(context, ccache.encode())
    krb5.cc_initialize(context, cache, user)
    krb5.cc_store_cred(context, cache, tgt)
    return ccache


class TestForwarded:
    def test_login_without_forwarded_tgt_captures_nothing(
        self, tmp_path: Path, krb5_env: None
    ) -> None:
        """Браузер TGT не прислал (AD не доверяет сервису): делегирования нет."""
        delegation = _forwarded()
        acceptor = SpnegoAcceptor(_accept(), delegation)
        identity = acceptor.accept(Browser.ticket(tmp_path))

        sign_in = TicketCapture(delegation).capture(identity)

        if sign_in is not None:
            raise AssertionError("forwarded mode must not accept a login without TGT")

    def test_tgt_ccache_matches_forwarded_mode(
        self, tmp_path: Path, krb5_env: None
    ) -> None:
        KerberosWorkspace.configure(str(KRB5_CONF), str(tmp_path / "cache"))
        credentials = KeytabCredentials.of(
            KeytabAuth(
                method="kerberos_keytab",
                principal=SERVICE_PRINCIPAL,
                keytab=str(SERVICE_KEYTAB),
            )
        )
        credentials.ensure()

        reason = TicketCapture.mismatch(
            credentials.ccache, SERVICE_PRINCIPAL, DelegationMode.FORWARDED
        )
        if reason:
            raise AssertionError(f"a TGT ccache must satisfy forwarded mode: {reason}")
