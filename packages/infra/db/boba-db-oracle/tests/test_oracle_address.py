"""Адреса Oracle: строка ↔ jsonb по таблице примеров, порт по умолчанию,
адрес базы по профилю соединения, совпадение форм с формулами ссылок скрапера."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from boba.connections.address import AddressError
from boba.db.oracle.address import OraAddresses, OraNodeKind
from boba.db.oracle.connection import OracleConfig, PasswordAuth

BASE = {"scheme": "oracle", "host": "db1", "port": 1521, "database": "orclpdb1"}

EXAMPLES: list[tuple[OraNodeKind, dict[str, object], str]] = [
    (OraNodeKind.DATABASE, {}, "oracle://db1:1521/orclpdb1"),
    (OraNodeKind.SCHEMA, {"schema": "HR"}, "oracle://db1:1521/orclpdb1?schema=HR"),
    (
        OraNodeKind.TABLE,
        {"schema": "HR", "table": "EMPLOYEES"},
        "oracle://db1:1521/orclpdb1?schema=HR&table=EMPLOYEES",
    ),
    (
        OraNodeKind.VIEW,
        {"schema": "HR", "view": "EMP_DETAILS_VIEW"},
        "oracle://db1:1521/orclpdb1?schema=HR&view=EMP_DETAILS_VIEW",
    ),
    (
        OraNodeKind.MVIEW,
        {"schema": "HR", "mview": "DAILY_SALES"},
        "oracle://db1:1521/orclpdb1?schema=HR&mview=DAILY_SALES",
    ),
    (
        OraNodeKind.COLUMN,
        {"schema": "HR", "table": "EMPLOYEES", "column": "EMAIL"},
        "oracle://db1:1521/orclpdb1?schema=HR&table=EMPLOYEES&column=EMAIL",
    ),
    (
        OraNodeKind.COLUMN,
        {"schema": "HR", "view": "EMP_DETAILS_VIEW", "column": "EMAIL"},
        "oracle://db1:1521/orclpdb1?schema=HR&view=EMP_DETAILS_VIEW&column=EMAIL",
    ),
    (
        OraNodeKind.COLUMN,
        {"schema": "HR", "mview": "DAILY_SALES", "column": "SALE_DAY"},
        "oracle://db1:1521/orclpdb1?schema=HR&mview=DAILY_SALES&column=SALE_DAY",
    ),
    (
        OraNodeKind.CONSTRAINT,
        {"schema": "HR", "table": "EMPLOYEES", "constraint": "EMP_EMP_ID_PK"},
        "oracle://db1:1521/orclpdb1?schema=HR&table=EMPLOYEES&constraint=EMP_EMP_ID_PK",
    ),
    (
        OraNodeKind.CONSTRAINT,
        {"schema": "HR", "view": "OPEN_ORDERS", "constraint": "OPEN_ORDERS_CK"},
        "oracle://db1:1521/orclpdb1?schema=HR&view=OPEN_ORDERS&constraint=OPEN_ORDERS_CK",
    ),
    (
        OraNodeKind.INDEX,
        {"schema": "HR", "table": "EMPLOYEES", "index": "EMP_NAME_IX"},
        "oracle://db1:1521/orclpdb1?schema=HR&table=EMPLOYEES&index=EMP_NAME_IX",
    ),
    (
        OraNodeKind.INDEX,
        {"schema": "HR", "mview": "DAILY_SALES", "index": "DAILY_SALES_DAY_IX"},
        "oracle://db1:1521/orclpdb1?schema=HR&mview=DAILY_SALES&index=DAILY_SALES_DAY_IX",
    ),
    (
        OraNodeKind.SEQUENCE,
        {"schema": "HR", "sequence": "EMPLOYEES_SEQ"},
        "oracle://db1:1521/orclpdb1?schema=HR&sequence=EMPLOYEES_SEQ",
    ),
    (
        OraNodeKind.SYNONYM,
        {"schema": "HR", "synonym": "CUST"},
        "oracle://db1:1521/orclpdb1?schema=HR&synonym=CUST",
    ),
    (
        OraNodeKind.TRIGGER,
        {"schema": "HR", "table": "EMPLOYEES", "trigger": "SECURE_EMPLOYEES"},
        "oracle://db1:1521/orclpdb1?schema=HR&table=EMPLOYEES&trigger=SECURE_EMPLOYEES",
    ),
    (
        OraNodeKind.TRIGGER,
        {"schema": "HR", "view": "OPEN_ORDERS", "trigger": "OPEN_ORDERS_IOI"},
        "oracle://db1:1521/orclpdb1?schema=HR&view=OPEN_ORDERS&trigger=OPEN_ORDERS_IOI",
    ),
    (
        OraNodeKind.ROUTINE,
        {"schema": "HR", "routine": "ORDER_API"},
        "oracle://db1:1521/orclpdb1?schema=HR&routine=ORDER_API",
    ),
]


@pytest.mark.parametrize(("kind", "roles", "text"), EXAMPLES)
def test_examples_round_trip(
    kind: OraNodeKind, roles: dict[str, object], text: str
) -> None:
    address = OraAddresses.parse(kind, text)

    assert kind == type(address).KIND
    assert address.to_json() == {**BASE, **roles}
    assert address.render() == text

    assert OraAddresses.parse_any(text) == address


def test_every_kind_has_a_form_in_examples() -> None:
    covered = {kind for kind, _, _ in EXAMPLES}

    assert covered == set(OraNodeKind)
    assert set(OraAddresses.kinds()) == set(OraNodeKind)


def test_port_defaults_to_listener() -> None:
    address = OraAddresses.parse(OraNodeKind.DATABASE, "oracle://db1/orclpdb1")

    assert address.render() == "oracle://db1:1521/orclpdb1"


def test_service_with_domain_and_quoted_names() -> None:
    text = "oracle://db1:1521/orclpdb1.localdomain?schema=HR&table=My%20Table"
    address = OraAddresses.parse(OraNodeKind.TABLE, text)

    assert address.to_json() == {
        **BASE,
        "database": "orclpdb1.localdomain",
        "schema": "HR",
        "table": "My Table",
    }
    assert address.render() == text


def test_kind_must_match_roles() -> None:
    with pytest.raises(AddressError, match="matches none of its shapes"):
        OraAddresses.parse(OraNodeKind.TABLE, "oracle://db1:1521/orclpdb1?view=V")


def test_postgres_scheme_is_refused() -> None:
    with pytest.raises(AddressError, match="expected scheme oracle"):
        OraAddresses.parse(OraNodeKind.DATABASE, "postgresql://db1:5432/orclpdb1")


def test_credentials_are_refused() -> None:
    with pytest.raises(AddressError, match="credentials"):
        OraAddresses.parse(OraNodeKind.DATABASE, "oracle://hr:x@db1:1521/orclpdb1")


def test_path_must_be_one_service() -> None:
    with pytest.raises(AddressError, match="single segment"):
        OraAddresses.parse(OraNodeKind.DATABASE, "oracle://db1:1521/a/b")


def _profile(**parts: object) -> OracleConfig:
    return OracleConfig.model_validate(
        {
            "host": "db1",
            "port": 1522,
            "service": "orclpdb1",
            "connect_timeout": 10,
            "call_timeout": 30000,
            "arraysize": 2000,
            "auth": PasswordAuth(
                method="password", user="app", password=SecretStr("x")
            ),
            **parts,
        }
    )


def test_base_of_profile() -> None:
    address = OraAddresses.base_of(_profile())

    assert address.render() == "oracle://db1:1522/orclpdb1"
