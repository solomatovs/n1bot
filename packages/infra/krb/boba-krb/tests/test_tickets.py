"""Билет одного вызова: выпуск из кредов приложения и применение телом."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Iterator
from pathlib import Path

import krb5
import pytest
from pydantic import SecretStr

from boba.krb import (
    ClientCredentials,
    CredentialsExpiredError,
    DelegatedConfig,
    DelegatedCredentials,
    DelegationMode,
    KerberosEnv,
    KerberosError,
    KeytabConfig,
    KeytabCredentials,
    ServiceTicketIssuer,
    TicketConfig,
    TicketCredentials,
    UserCcache,
)

_KRB = Path(__file__).resolve().parents[5] / "compose" / "conf" / "krb"
KEYTAB = _KRB / "boba-svc.keytab"
KRB5_CONF = _KRB / "krb5.conf"
PRINCIPAL = "boba-svc@LOSHARA.COM"
SERVICE = "postgres@postgres-17.loshara.com"
SERVER = "postgres/postgres-17.loshara.com@LOSHARA.COM"

live_kdc = pytest.mark.skipif(
    not KEYTAB.is_file() or not KRB5_CONF.is_file(),
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


def _source(ccache: Path) -> KeytabCredentials:
    return KeytabCredentials.of(
        KeytabConfig(
            keytab=str(KEYTAB),
            principal=PRINCIPAL,
            ccache=f"FILE:{ccache}",
            krb5_config=str(KRB5_CONF),
        )
    )


def _servers(ccache: str) -> list[str]:
    context = krb5.init_context()
    cache = krb5.cc_resolve(context, ccache.encode())
    return [krb5.unparse_name_flags(context, cred.server).decode() for cred in cache]


class TestServerPrefix:
    def test_hostbased_to_principal_prefix(self) -> None:
        prefix = ServiceTicketIssuer.server_prefix(SERVICE)
        if prefix != "postgres/postgres-17.loshara.com@":
            raise AssertionError("prefix must be service/host@")

    def test_service_without_host_rejected(self) -> None:
        with pytest.raises(KerberosError, match="service@host"):
            ServiceTicketIssuer.server_prefix("postgres")


class TestTicketConfig:
    def test_ccache_must_be_base64(self) -> None:
        with pytest.raises(ValueError, match="base64"):
            TicketConfig(principal="u@R", service=SERVICE, ccache=SecretStr("%%%"))

    def test_blob_hidden_without_reveal(self) -> None:
        ticket = TicketConfig.of_bytes("u@R", SERVICE, b"secret-bytes", 60)
        dump = json.dumps(ticket.model_dump(mode="json"))
        if base64.b64encode(b"secret-bytes").decode() in dump:
            raise AssertionError("ticket bytes leaked into a plain dump")
        if "secret-bytes" in repr(ticket):
            raise AssertionError("ticket bytes leaked into repr")

    def test_blob_revealed_for_the_sandbox(self) -> None:
        ticket = TicketConfig.of_bytes("u@R", SERVICE, b"secret-bytes", 60)
        reveal = {TicketConfig.REVEAL_SECRETS: True}
        dump = ticket.model_dump(mode="json", context=reveal)
        if dump["ccache"] != base64.b64encode(b"secret-bytes").decode():
            raise AssertionError("revealed dump must carry the bytes")
        if TicketConfig.model_validate(dump).ccache_bytes() != b"secret-bytes":
            raise AssertionError("revealed dump must validate back")


class TestDelegatedConfigStaysOutside:
    def test_body_refuses_delegated_section(self) -> None:
        with pytest.raises(KerberosError, match="resolved by the application"):
            ClientCredentials.of(DelegatedConfig())


@live_kdc
class TestServiceTicketIssuer:
    def test_ccache_holds_only_the_service_ticket(
        self, tmp_path: Path, clean_env: None
    ) -> None:
        ticket = ServiceTicketIssuer(min_lifetime=60).issue(
            _source(tmp_path / "src"), SERVICE
        )

        credentials = TicketCredentials(ticket)
        with credentials.applied():
            servers = _servers(credentials.ccache)

        if servers != [SERVER]:
            raise AssertionError(f"ticket ccache must hold one ticket: {servers}")

    def test_source_keeps_its_tgt(self, tmp_path: Path, clean_env: None) -> None:
        source = _source(tmp_path / "src")
        ServiceTicketIssuer(min_lifetime=60).issue(source, SERVICE)

        servers = _servers(source.ccache)
        if not any(server.startswith("krbtgt/") for server in servers):
            raise AssertionError(f"source must keep its TGT: {servers}")

    def test_ticket_principal_and_service(
        self, tmp_path: Path, clean_env: None
    ) -> None:
        ticket = ServiceTicketIssuer(min_lifetime=60).issue(
            _source(tmp_path / "src"), SERVICE
        )
        if (ticket.principal, ticket.service) != (PRINCIPAL, SERVICE):
            raise AssertionError("ticket must name the source principal and the SPN")

    def test_relabelled_ccache_is_refused(
        self, tmp_path: Path, clean_env: None
    ) -> None:
        """Ccache под чужим принципалом билет за другого не выпускает."""
        source = _source(tmp_path / "src")
        source.ensure()
        relabelled = DelegatedCredentials(
            UserCcache("other@LOSHARA.COM", source.ccache, "login-1"),
            mode=DelegationMode.FORWARDED,
            renew=False,
            krb5_config=str(KRB5_CONF),
        )

        with pytest.raises(CredentialsExpiredError, match="expired"):
            ServiceTicketIssuer(min_lifetime=60).issue(relabelled, SERVICE)

    def test_unknown_service_is_refused(self, tmp_path: Path, clean_env: None) -> None:
        with pytest.raises(KerberosError):
            ServiceTicketIssuer(min_lifetime=60).issue(
                _source(tmp_path / "src"), "nosuch@nowhere.loshara.com"
            )


@live_kdc
class TestTicketCredentials:
    def test_applied_exposes_private_file(
        self, tmp_path: Path, clean_env: None
    ) -> None:
        ticket = ServiceTicketIssuer(min_lifetime=60).issue(
            _source(tmp_path / "src"), SERVICE
        )
        credentials = ClientCredentials.of(ticket)
        if not isinstance(credentials, TicketCredentials):
            raise AssertionError("ticket config must build TicketCredentials")

        with credentials.applied():
            path = os.environ[KerberosEnv.CCACHE].removeprefix("FILE:")
            mode = os.stat(path).st_mode & 0o777
            if mode != 0o600:
                raise AssertionError(f"ticket file must be private: {oct(mode)}")
            if KerberosEnv.CONFIG in os.environ:
                raise AssertionError("krb5.conf is read from its standard path")

        if os.path.exists(path):
            raise AssertionError("leaving applied() must remove the ticket file")
        if KerberosEnv.CCACHE in os.environ:
            raise AssertionError("leaving applied() must restore the environment")

    def test_ccache_is_unavailable_outside_applied(
        self, tmp_path: Path, clean_env: None
    ) -> None:
        ticket = ServiceTicketIssuer(min_lifetime=60).issue(
            _source(tmp_path / "src"), SERVICE
        )
        credentials = TicketCredentials(ticket)

        with pytest.raises(KerberosError, match="inside applied"):
            _ = credentials.ccache

    def test_expired_ticket_refused(self, tmp_path: Path, clean_env: None) -> None:
        ticket = ServiceTicketIssuer(min_lifetime=60).issue(
            _source(tmp_path / "src"), SERVICE
        )
        strict = ticket.model_copy(update={"min_lifetime": 10**9})

        credentials = TicketCredentials(strict)
        with pytest.raises(CredentialsExpiredError):
            credentials.ensure()

    def test_garbage_blob_has_no_lifetime(self) -> None:
        ticket = TicketConfig.of_bytes("u@R", SERVICE, b"not a ccache", 60)

        credentials = TicketCredentials(ticket)
        with pytest.raises(CredentialsExpiredError):
            credentials.ensure()
