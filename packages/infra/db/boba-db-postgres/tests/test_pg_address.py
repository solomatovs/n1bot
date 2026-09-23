"""Адреса PostgreSQL: строка ↔ jsonb по таблице примеров, отказы разбора,
адрес базы по профилю соединения."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from boba.connections.address import AddressError
from boba.db.postgres.address import PgAddresses, PgNodeKind
from boba.db.postgres.connection import (
    PasswordAuth,
    PostgresConfig,
    PostgresOptionsConfig,
    PostgresPoolConfig,
)

BASE = {"scheme": "postgresql", "host": "dwh.local", "port": 5432, "database": "dwh"}

EXAMPLES: list[tuple[PgNodeKind, dict[str, object], str]] = [
    (PgNodeKind.DATABASE, {}, "postgresql://dwh.local:5432/dwh"),
    (PgNodeKind.SCHEMA, {"schema": "dm"}, "postgresql://dwh.local:5432/dwh?schema=dm"),
    (
        PgNodeKind.TABLE,
        {"schema": "dm", "table": "fact_orders"},
        "postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders",
    ),
    (
        PgNodeKind.COLUMN,
        {"schema": "dm", "table": "fact_orders", "column": "amount"},
        "postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders&column=amount",
    ),
    (
        PgNodeKind.VIEW,
        {"schema": "dm", "view": "v_orders_daily"},
        "postgresql://dwh.local:5432/dwh?schema=dm&view=v_orders_daily",
    ),
    (
        PgNodeKind.COLUMN,
        {"schema": "dm", "view": "v_orders_daily", "column": "day"},
        "postgresql://dwh.local:5432/dwh?schema=dm&view=v_orders_daily&column=day",
    ),
    (
        PgNodeKind.MATVIEW,
        {"schema": "dm", "matview": "mv_orders_month"},
        "postgresql://dwh.local:5432/dwh?schema=dm&matview=mv_orders_month",
    ),
    (
        PgNodeKind.COLUMN,
        {"schema": "dm", "matview": "mv_orders_month", "column": "month"},
        "postgresql://dwh.local:5432/dwh?schema=dm&matview=mv_orders_month&column=month",
    ),
    (
        PgNodeKind.INDEX,
        {"schema": "dm", "index": "fact_orders_customer_idx"},
        "postgresql://dwh.local:5432/dwh?schema=dm&index=fact_orders_customer_idx",
    ),
    (
        PgNodeKind.SEQUENCE,
        {"schema": "dm", "sequence": "fact_orders_order_id_seq"},
        "postgresql://dwh.local:5432/dwh?schema=dm&sequence=fact_orders_order_id_seq",
    ),
    (
        PgNodeKind.FUNCTION,
        {"schema": "dm", "function": "calc_total", "args": "bigint,numeric"},
        "postgresql://dwh.local:5432/dwh?schema=dm&function=calc_total&args=bigint%2Cnumeric",
    ),
    (
        PgNodeKind.FUNCTION,
        {"schema": "dm", "function": "now_utc", "args": ""},
        "postgresql://dwh.local:5432/dwh?schema=dm&function=now_utc&args=",
    ),
    (
        PgNodeKind.PROCEDURE,
        {"schema": "dm", "procedure": "close_orders", "args": "date,text"},
        "postgresql://dwh.local:5432/dwh?schema=dm&procedure=close_orders&args=date%2Ctext",
    ),
    (
        PgNodeKind.ROUTINE,
        {"schema": "dm", "routine": "array_median", "args": "numeric[]"},
        "postgresql://dwh.local:5432/dwh?schema=dm&routine=array_median&args=numeric%5B%5D",
    ),
    (
        PgNodeKind.CONSTRAINT,
        {
            "schema": "dm",
            "table": "fact_orders",
            "constraint": "fact_orders_customer_fkey",
        },
        "postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders&constraint=fact_orders_customer_fkey",
    ),
    (
        PgNodeKind.TRIGGER,
        {"schema": "dm", "table": "fact_orders", "trigger": "trg_orders_audit"},
        "postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders&trigger=trg_orders_audit",
    ),
]


@pytest.mark.parametrize(("kind", "roles", "text"), EXAMPLES)
def test_examples_round_trip(
    kind: PgNodeKind, roles: dict[str, object], text: str
) -> None:
    address = PgAddresses.parse(kind, text)

    assert kind == type(address).KIND
    assert address.to_json() == {**BASE, **roles}
    assert address.render() == text

    same = PgAddresses.parse_any(text)
    assert same == address


def test_roles_in_any_order_render_canonical() -> None:
    text = "postgresql://dwh.local:5432/dwh?column=amount&table=fact_orders&schema=dm"

    address = PgAddresses.parse(PgNodeKind.COLUMN, text)

    assert (
        address.render()
        == "postgresql://dwh.local:5432/dwh?schema=dm&table=fact_orders&column=amount"
    )


def test_port_defaults_to_libpq() -> None:
    address = PgAddresses.parse(PgNodeKind.DATABASE, "postgresql://dwh.local/dwh")

    assert address.render() == "postgresql://dwh.local:5432/dwh"


def test_kind_must_match_roles() -> None:
    with pytest.raises(AddressError, match="matches none of its shapes"):
        PgAddresses.parse(
            PgNodeKind.TABLE,
            "postgresql://dwh.local:5432/dwh?schema=dm&view=v_orders_daily",
        )


def test_unknown_roles_are_refused() -> None:
    with pytest.raises(AddressError, match="matches no object shape"):
        PgAddresses.parse_any("postgresql://dwh.local:5432/dwh?tbl=fact_orders")


@pytest.mark.parametrize(
    "text",
    [
        "postgresql://user:pass@dwh.local:5432/dwh",
        "postgresql://dwh.local:5432/dwh#frag",
        "postgresql://dwh.local:5432/dwh/extra",
        "postgresql://dwh.local:5432/",
        "postgresql://:5432/dwh",
        "postgresql://dwh.local:port/dwh",
        "mysql://dwh.local:5432/dwh",
    ],
)
def test_malformed_url_is_refused(text: str) -> None:
    with pytest.raises(AddressError):
        PgAddresses.parse(PgNodeKind.DATABASE, text)


def test_repeated_role_is_refused() -> None:
    with pytest.raises(AddressError, match="repeats a role"):
        PgAddresses.parse(
            PgNodeKind.SCHEMA, "postgresql://dwh.local:5432/dwh?schema=dm&schema=dm"
        )


def test_empty_role_value_is_refused() -> None:
    with pytest.raises(AddressError, match="is not valid"):
        PgAddresses.parse(PgNodeKind.SCHEMA, "postgresql://dwh.local:5432/dwh?schema=")


def test_special_characters_are_quoted() -> None:
    text = "postgresql://dwh.local:5432/my%20db?schema=dm&table=a%26b"

    address = PgAddresses.parse(PgNodeKind.TABLE, text)

    assert address.to_json() == {
        **BASE,
        "database": "my db",
        "schema": "dm",
        "table": "a&b",
    }
    assert address.render() == text


def test_prompt_lists_every_kind() -> None:
    prompt = PgAddresses.prompt()

    for kind in PgNodeKind:
        assert f"{kind}:" in prompt

    assert "pg_column: schema, table, column | schema, view, column" in prompt


def _profile(**parts: object) -> PostgresConfig:
    return PostgresConfig.model_validate(
        {
            "dbname": "dwh",
            "auth": PasswordAuth(
                method="password", user="app", password=SecretStr("x")
            ),
            "options": PostgresOptionsConfig(),
            "pool": PostgresPoolConfig(),
            **parts,
        }
    )


def test_base_of_profile() -> None:
    address = PgAddresses.base_of(_profile(host="dwh.local", port=6432))

    assert address.render() == "postgresql://dwh.local:6432/dwh"


def test_base_of_profile_without_port_uses_libpq_default() -> None:
    address = PgAddresses.base_of(_profile(host="dwh.local"))

    assert address.render() == "postgresql://dwh.local:5432/dwh"


def test_base_of_multi_host_takes_first() -> None:
    address = PgAddresses.base_of(_profile(host="dwh1.local,dwh2.local"))

    assert address.render() == "postgresql://dwh1.local:5432/dwh"


def test_base_of_hostaddr_only() -> None:
    address = PgAddresses.base_of(_profile(hostaddr="10.0.0.5"))

    assert address.render() == "postgresql://10.0.0.5:5432/dwh"
