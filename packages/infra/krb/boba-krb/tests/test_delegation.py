"""Делегирование входа на живом KDC стенда: forwarded и constrained.

Браузер моделируется initiate-контекстом пользователя `readonly` (TGT по
паролю из [site]); сервис — accept по keytab boba-svc. Constrained требует
на стенде `samba-tool delegation add-service boba-svc postgres/…` и снятого
флага sensitive у boba-svc.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterator
from pathlib import Path

import krb5
import pytest
from gssapi import Credentials, Name, NameType, SecurityContext

from boba.krb import (
    AcceptConfig,
    CcacheLifetime,
    CcacheRegistry,
    ConstrainedDelegation,
    DelegationMode,
    ForwardedDelegation,
    KerberosDelegation,
    KerberosEnv,
    KeytabConfig,
    KeytabCredentials,
    ServiceTicketIssuer,
    SpnegoAcceptor,
    TicketCredentials,
)

_REPO = Path(__file__).resolve().parents[5]
_KRB = _REPO / "compose" / "conf" / "krb"
_CONFIG = _REPO / "compose" / "conf" / "config.toml"
KRB5_CONF = _KRB / "krb5.conf"
SERVICE_KEYTAB = _KRB / "boba-svc.keytab"
SERVICE_SPN = "HTTP/loshara.com@LOSHARA.COM"
SERVICE_PRINCIPAL = "boba-svc@LOSHARA.COM"
USER_PRINCIPAL = "readonly@LOSHARA.COM"
TARGET = "postgres@postgres-17.loshara.com"

live_kdc = pytest.mark.skipif(
    not SERVICE_KEYTAB.is_file() or not KRB5_CONF.is_file() or not _CONFIG.is_file(),
    reason="нет keytab/krb5.conf/config.toml стенда",
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
    with _CONFIG.open("rb") as handle:
        return str(tomllib.load(handle)["site"]["ldap_bind_password"])


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


def _accept() -> AcceptConfig:
    return AcceptConfig(service_name=SERVICE_SPN, keytab=str(SERVICE_KEYTAB))


def _constrained(tmp_path: Path) -> ConstrainedDelegation:
    return ConstrainedDelegation(
        ccache_template=f"FILE:{tmp_path}/login-{{login}}",
        service_ccache=f"FILE:{tmp_path / 'service'}",
        krb5_config=str(KRB5_CONF),
    )


def _forwarded(tmp_path: Path) -> ForwardedDelegation:
    return ForwardedDelegation(
        ccache_template=f"FILE:{tmp_path}/login-{{login}}",
        renew=False,
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
        registry = CcacheRegistry(
            mode=DelegationMode.CONSTRAINED, renew=False, krb5_config=str(KRB5_CONF)
        )
        acceptor = SpnegoAcceptor(_accept(), delegation)
        identity = acceptor.accept(Browser.ticket(tmp_path))
        if identity.principal != USER_PRINCIPAL:
            raise AssertionError(f"accepted someone else: {identity.principal}")

        capture = KerberosDelegation(registry, _accept(), delegation)
        login = capture.on_success_authenticated(identity)
        if not login:
            raise AssertionError("constrained login must capture evidence credentials")

        credentials = registry.of_login(login)
        if credentials is None:
            raise AssertionError("registry must know the login")
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
        ccache = f"FILE:{tmp_path / 'forwarded'}"
        KeytabCredentials.of(
            KeytabConfig(
                keytab=str(SERVICE_KEYTAB),
                principal=SERVICE_PRINCIPAL,
                ccache=ccache,
                krb5_config=str(KRB5_CONF),
            )
        ).ensure()

        reason = KerberosDelegation.mismatch(
            ccache, SERVICE_PRINCIPAL, DelegationMode.CONSTRAINED
        )
        if "forwarded TGT" not in reason:
            raise AssertionError(f"TGT must be rejected: {reason!r}")


class TestForwarded:
    def test_login_without_forwarded_tgt_captures_nothing(
        self, tmp_path: Path, krb5_env: None
    ) -> None:
        """Браузер TGT не прислал (AD не доверяет сервису): делегирования нет."""
        delegation = _forwarded(tmp_path)
        registry = CcacheRegistry(
            mode=DelegationMode.FORWARDED, renew=False, krb5_config=str(KRB5_CONF)
        )
        acceptor = SpnegoAcceptor(_accept(), delegation)
        identity = acceptor.accept(Browser.ticket(tmp_path))

        capture = KerberosDelegation(registry, _accept(), delegation)
        login = capture.on_success_authenticated(identity)

        if login:
            raise AssertionError("forwarded mode must not accept a login without TGT")

    def test_tgt_ccache_matches_forwarded_mode(
        self, tmp_path: Path, krb5_env: None
    ) -> None:
        ccache = f"FILE:{tmp_path / 'tgt'}"
        KeytabCredentials.of(
            KeytabConfig(
                keytab=str(SERVICE_KEYTAB),
                principal=SERVICE_PRINCIPAL,
                ccache=ccache,
                krb5_config=str(KRB5_CONF),
            )
        ).ensure()

        reason = KerberosDelegation.mismatch(
            ccache, SERVICE_PRINCIPAL, DelegationMode.FORWARDED
        )
        if reason:
            raise AssertionError(f"a TGT ccache must satisfy forwarded mode: {reason}")
