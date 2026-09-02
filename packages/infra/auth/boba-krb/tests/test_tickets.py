"""Билет одного вызова: выпуск из кредов приложения и применение телом."""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import krb5
import pytest
from pydantic import SecretStr

from boba.kerberos import (
    CredentialsExpiredError,
    DelegatedAuth,
    DelegationMode,
    KerberosError,
    KeytabAuth,
    SignInTicket,
    TicketAuth,
)
from boba.krb import (
    ClientCredentials,
    DelegatedCredentials,
    KerberosEnv,
    KerberosWorkspace,
    KeytabCredentials,
    ServiceTicketIssuer,
    TicketCredentials,
)
from boba.stand.site import Stand

STAND = Stand.required()
KEYTAB = Path(STAND.krb_pg_keytab)
KRB5_CONF = Path(STAND.krb_config)
PRINCIPAL = STAND.service_principal
SERVICE = STAND.pg_spn
SERVER = f"{STAND.pg_krbsrvname}/{STAND.pg_host}@{STAND.krb_realm}"
OTHER_PRINCIPAL = f"other@{STAND.krb_realm}"

live_kdc = pytest.mark.skipif(
    not STAND.live(),
    reason="нет keytab/krb5.conf локального AD",
)


@pytest.fixture
def clean_env() -> Iterator[None]:
    names = (KerberosEnv.CCACHE, KerberosEnv.CLIENT_KEYTAB, KerberosEnv.CONFIG)
    saved = {name: os.environ.get(name) for name in names}

    for name in names:
        os.environ.pop(name, None)

    yield

    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
            continue
        os.environ[name] = value


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path) -> Iterator[None]:
    """Каталог кэшей на тест; после — откат: workspace глобален на процесс."""
    with KerberosWorkspace.scoped(str(KRB5_CONF), str(tmp_path / "cache")):
        yield


def _source() -> KeytabCredentials:
    return KeytabCredentials.of(
        KeytabAuth(
            method="kerberos_keytab",
            principal=PRINCIPAL,
            keytab=str(KEYTAB),
        )
    )


def _servers(ccache: str) -> list[str]:
    context = krb5.init_context()
    cache = krb5.cc_resolve(context, ccache.encode())
    return [krb5.unparse_name_flags(context, cred.server).decode() for cred in cache]


class TestServerPrefix:
    def test_hostbased_to_principal_prefix(self) -> None:
        prefix = ServiceTicketIssuer.server_prefix(SERVICE)
        if prefix != f"{STAND.pg_krbsrvname}/{STAND.pg_host}@":
            raise AssertionError("prefix must be service/host@")

    def test_service_without_host_rejected(self) -> None:
        with pytest.raises(KerberosError, match="service@host"):
            ServiceTicketIssuer.server_prefix(STAND.pg_krbsrvname)


class TestTicketConfig:
    def test_ccache_must_be_base64(self) -> None:
        with pytest.raises(ValueError, match="base64"):
            TicketAuth(
                method="kerberos_ticket",
                principal="u@R",
                service=SERVICE,
                ccache=SecretStr("%%%"),
            )

    def test_blob_hidden_without_reveal(self) -> None:
        ticket = TicketAuth.of_bytes("u@R", SERVICE, b"secret-bytes", 60)
        dump = json.dumps(ticket.model_dump(mode="json"))
        if base64.b64encode(b"secret-bytes").decode() in dump:
            raise AssertionError("ticket bytes leaked into a plain dump")
        if "secret-bytes" in repr(ticket):
            raise AssertionError("ticket bytes leaked into repr")

    def test_blob_revealed_for_the_sandbox(self) -> None:
        ticket = TicketAuth.of_bytes("u@R", SERVICE, b"secret-bytes", 60)
        reveal = {TicketAuth.REVEAL_SECRETS: True}
        dump = ticket.model_dump(mode="json", context=reveal)
        if dump["ccache"] != base64.b64encode(b"secret-bytes").decode():
            raise AssertionError("revealed dump must carry the bytes")
        if TicketAuth.model_validate(dump).ccache_bytes() != b"secret-bytes":
            raise AssertionError("revealed dump must validate back")


class TestDelegatedConfigStaysOutside:
    def test_body_refuses_delegated_section(self) -> None:
        with pytest.raises(KerberosError, match="resolved by the application"):
            ClientCredentials.of(DelegatedAuth(method="kerberos_delegated"))


@live_kdc
class TestServiceTicketIssuer:
    def test_ccache_holds_only_the_service_ticket(self, clean_env: None) -> None:
        ticket = ServiceTicketIssuer(min_lifetime=60).issue(_source(), SERVICE)

        credentials = TicketCredentials(ticket)
        with credentials.applied():
            servers = _servers(credentials.ccache)

        if servers != [SERVER]:
            raise AssertionError(f"one ticket expected: {servers}")

    def test_source_keeps_its_tgt(self, clean_env: None) -> None:
        source = _source()
        ServiceTicketIssuer(min_lifetime=60).issue(source, SERVICE)

        servers = _servers(source.ccache)
        if not any(server.startswith("krbtgt/") for server in servers):
            raise AssertionError(f"source must keep its TGT: {servers}")

    def test_ticket_principal_and_service(self, clean_env: None) -> None:
        ticket = ServiceTicketIssuer(min_lifetime=60).issue(_source(), SERVICE)
        if (ticket.principal, ticket.service) != (PRINCIPAL, SERVICE):
            raise AssertionError("ticket must name the source principal and the SPN")

    def test_relabelled_ccache_is_refused(self, clean_env: None) -> None:
        """Ccache под чужим принципалом билет за другого не выпускает."""
        source = _source()
        source.ensure()
        with open(source.ccache.removeprefix("FILE:"), "rb") as cache:
            data = cache.read()
        relabelled = DelegatedCredentials(
            SignInTicket(
                principal=OTHER_PRINCIPAL,
                mode=DelegationMode.FORWARDED,
                ccache=data,
                expires_at=int(time.time()) + 600,
            ),
            str(KRB5_CONF),
        )
        with pytest.raises(KerberosError, match="belongs to"):
            ServiceTicketIssuer(min_lifetime=60).issue(relabelled, SERVICE)

    def test_unknown_service_is_refused(self, clean_env: None) -> None:
        with pytest.raises(KerberosError):
            ServiceTicketIssuer(min_lifetime=60).issue(
                _source(), f"nosuch@nowhere.{STAND.krb_domain}"
            )


@live_kdc
class TestTicketCredentials:
    def test_applied_exposes_private_file(self, clean_env: None) -> None:
        ticket = ServiceTicketIssuer(min_lifetime=60).issue(_source(), SERVICE)
        credentials = ClientCredentials.of(ticket)
        if not isinstance(credentials, TicketCredentials):
            raise AssertionError("ticket config must build TicketCredentials")

        with credentials.applied():
            path = os.environ[KerberosEnv.CCACHE].removeprefix("FILE:")
            mode = os.stat(path).st_mode & 0o777
            if mode != 0o600:
                raise AssertionError(f"ticket file must be private: {oct(mode)}")
            if KerberosEnv.CONFIG in os.environ:
                raise AssertionError("krb5.conf is the sandbox's own, not the host's")

        if os.path.exists(path):
            raise AssertionError("leaving applied() must remove the ticket file")
        if KerberosEnv.CCACHE in os.environ:
            raise AssertionError("leaving applied() must restore the environment")

    def test_ccache_is_unavailable_outside_applied(self, clean_env: None) -> None:
        ticket = ServiceTicketIssuer(min_lifetime=60).issue(_source(), SERVICE)
        credentials = TicketCredentials(ticket)

        with pytest.raises(KerberosError, match="inside applied"):
            _ = credentials.ccache

    def test_expired_ticket_refused(self, tmp_path: Path, clean_env: None) -> None:
        ticket = ServiceTicketIssuer(min_lifetime=60).issue(_source(), SERVICE)
        strict = ticket.model_copy(update={"min_lifetime": 10**9})

        credentials = TicketCredentials(strict)
        with pytest.raises(CredentialsExpiredError):
            credentials.ensure()

    def test_garbage_blob_has_no_lifetime(self) -> None:
        ticket = TicketAuth.of_bytes("u@R", SERVICE, b"not a ccache", 60)

        credentials = TicketCredentials(ticket)
        with pytest.raises(CredentialsExpiredError):
            credentials.ensure()
