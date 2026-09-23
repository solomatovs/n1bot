"""Стенд инструментов Oracle: секция [ix_stand].ora_sources и схема TOOL_DEMO с
парой таблиц, которую тест пересоздаёт администратором цели.

Лежит отдельным модулем, а не в conftest: имя conftest у каждого пакета своё, и при
общем прогоне нескольких пакетов импорт из него достаётся чужому файлу.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import ClassVar

from oracledb import AsyncConnection
from pydantic import BaseModel, ConfigDict, SecretStr

from boba.db.oracle import OracleQueryError
from boba.db.oracle.connection import OracleConfig, PasswordAuth
from boba.db.oracle.payload import PayloadOracle
from boba.stand.ix import IxStand as SharedIxStand
from boba.stand.ix import IxStandError

__all__ = ["DemoUser", "IxSource", "IxStand", "ToolDemo"]


class DemoUser(StrEnum):
    """Схема демонстрационного набора инструментов и её пароль на стенде."""

    NAME = "TOOL_DEMO"
    PASSWORD = "tool_demo"


class IxSource(BaseModel):
    """Один источник стенда: имя цели, профиль с минимальными правами и профиль
    администратора, которым пересоздаётся схема TOOL_DEMO."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    oracle: OracleConfig
    admin: OracleConfig

    @property
    def demo_owner(self) -> OracleConfig:
        auth = PasswordAuth(
            method="password",
            user=DemoUser.NAME.value,
            password=SecretStr(DemoUser.PASSWORD.value),
        )
        return self.admin.model_copy(update={"auth": auth})


class IxStand(SharedIxStand):
    """Секция [ix_stand] инструментов Oracle: общий стенд ix плюс ora_sources."""

    ora_sources: Sequence[IxSource]

    def source(self, name: str) -> IxSource:
        for item in self.ora_sources:
            if item.name == name:
                return item

        raise IxStandError(
            f"ix stand: oracle source {name!r} is not listed in [ix_stand]"
        )


class ToolDemo:
    """Схема TOOL_DEMO: таблица с ключами и комментариями, представление,
    последовательность, функция и таблица-приёмник для насоса. Пользователя
    пересоздаёт администратор, объекты создаёт сам TOOL_DEMO."""

    NO_SUCH_USER: ClassVar[str] = "ORA-01918"

    ADMIN: ClassVar[tuple[str, ...]] = (
        f"drop user {DemoUser.NAME} cascade",
        f"create user {DemoUser.NAME} identified by {DemoUser.PASSWORD} "
        "default tablespace users quota unlimited on users",
        "grant create session, create table, create view, create sequence, "
        f"create procedure to {DemoUser.NAME}",
    )

    OWNER: ClassVar[tuple[str, ...]] = (
        "create table customers ("
        " id number(10) not null,"
        " email varchar2(200) not null,"
        " balance number(18, 2) default 0 not null,"
        " created_at timestamp(6) not null,"
        " note varchar2(200),"
        " photo raw(16),"
        " constraint customers_pk primary key (id),"
        " constraint customers_email_uk unique (email),"
        " constraint customers_balance_ck check (balance >= 0))",
        "comment on table customers is 'Клиенты'",
        "comment on column customers.email is 'Почта, уникальна'",
        "create index customers_note_ix on customers (note)",
        "create table orders ("
        " id number(12) not null,"
        " customer_id number(10) not null,"
        " amount number(18, 2) not null,"
        " constraint orders_pk primary key (id),"
        " constraint orders_customer_fk foreign key (customer_id)"
        "  references customers (id))",
        "create view customer_orders as"
        " select c.id, c.email, count(o.id) as orders_count"
        " from customers c left join orders o on o.customer_id = c.id"
        " group by c.id, c.email",
        "create sequence customer_seq start with 100 increment by 5",
        "create or replace function order_total(p_id in number) return number is"
        " v number; begin select sum(amount) into v from orders where id = p_id;"
        " return nvl(v, 0); end;",
        "create table sink ("
        " id number(10), email varchar2(200), balance number(18, 2),"
        " created_at timestamp(6), note varchar2(200), photo raw(16))",
    )

    GRANTS: ClassVar[tuple[str, ...]] = (
        "grant select on customers to {reader}",
        "grant select on orders to {reader}",
        "grant select on customer_orders to {reader}",
        "grant select on customer_seq to {reader}",
        "grant execute on order_total to {reader}",
        "grant select, insert on sink to {reader}",
    )
    """Права учётки с минимальными правами: без них all_* не покажет объекты."""

    FILL: ClassVar[str] = (
        "insert into customers (id, email, balance, created_at, note, photo)"
        " select level, 'user' || level || '@example.com', level / 4,"
        " timestamp '2024-02-29 13:14:15.123456' + numtodsinterval(level, 'second'),"
        " case when mod(level, 3) = 0 then null else 'note, \"' || level || '\"' end,"
        " case when mod(level, 5) = 0 then null else hextoraw('00ff10') end"
        " from dual connect by level <= :n"
    )

    def __init__(self, source: IxSource) -> None:
        self._source = source

    async def recreate(self, rows: int) -> None:
        payload = PayloadOracle(self._source.admin)
        async with payload.opened() as admin:
            await self._recreate_user(payload, admin)
        await self._fill(rows)

    async def drop(self) -> None:
        """Снести схему после тестов: стенд общий со скрапером словаря, и лишняя
        схема ломает его эталонные отпечатки."""
        payload = PayloadOracle(self._source.admin)
        async with payload.opened() as admin:
            await self._drop_user(payload, admin)

    async def _fill(self, rows: int) -> None:

        owner = PayloadOracle(self._source.demo_owner)
        async with owner.opened() as conn:
            for statement in self.OWNER:
                await self._run(owner, conn, statement)

            reader = self._source.oracle.auth.user
            for template in self.GRANTS:
                await self._run(owner, conn, template.format(reader=reader))

            async with owner.rows(conn, self.FILL, {"n": rows}):
                pass

            await owner.commit(conn)

    async def _recreate_user(
        self, payload: PayloadOracle, admin: AsyncConnection
    ) -> None:
        await self._drop_user(payload, admin)

        _, *rest = self.ADMIN
        for statement in rest:
            await self._run(payload, admin, statement)

    async def _drop_user(self, payload: PayloadOracle, admin: AsyncConnection) -> None:
        drop, *_ = self.ADMIN
        try:
            await self._run(payload, admin, drop)
        except OracleQueryError as exc:
            if self.NO_SUCH_USER not in str(exc):
                raise

    @staticmethod
    async def _run(payload: PayloadOracle, conn: AsyncConnection, text: str) -> None:
        async with payload.rows(conn, text):
            pass
