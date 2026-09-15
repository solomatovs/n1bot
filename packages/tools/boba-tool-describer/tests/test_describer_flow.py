"""Сквозной сценарий describer внутри хода (pytest -m integration).

Инструменты собираются боевым ChatPlugins.load и работают в зиготах секций;
соединения пользователя лежат в таблицах брокера; модель — по сценарию.
Ход повторяет работу агента: connection_list → pg_describe_table и pg_query
по pg_constraint → ch_describe_table → базовые url соединений → узлы и
рёбра describe_* (в том числе кроссбазное ребро pg ↔ ch и понятие entity) →
ошибочные вызовы, которые ход переживает → describe_list_nodes и
describe_list_edges → удаление по id → ответ. Строки
проверяются прямым запросом к таблицам node/edge тестовой базы.
"""

from __future__ import annotations

import base64
import os
import secrets as std_secrets
import shutil
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

import chainlit as cl
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from omegaconf import DictConfig, OmegaConf
from psycopg import sql
from pydantic import SecretStr

from boba.chainlit.agent.flow import GraphSpec, PlainGraphBuilder
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.infra.plugins import ChatPlugins
from boba.chainlit.infra.providers import build_history_view
from boba.config import bind
from boba.connection_broker.store import ConnectionsConfig, ConnectionStore
from boba.connections.manifest import ConnectionTypes
from boba.connections.profile import GrantTarget
from boba.db.clickhouse.address import ChAddresses, ChTableColumnAddress
from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.clickhouse.profile import ClickHouseConfig
from boba.db.clickhouse.profile import PasswordAuth as ChPasswordAuth
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.address import (
    PgAddresses,
    PgTableAddress,
    PgTableColumnAddress,
)
from boba.db.postgres.profile import PostgresConfig
from boba.runtime.config import AppLayers, ConfigLocator
from boba.sandbox import ZygoteRegistry
from boba.stand.refs import StandRefs
from boba.stand.site import Stand
from boba.stand_core.context import use_context
from boba.tool.describer.address import Addresses, EntityAddress
from boba.tool.describer.edges import EdgeKind, EdgeListColumn
from boba.tool.describer.nodes import NodeListColumn
from boba.toolkit.result import ErrorResult, SqlResult, TableResult, ToolArtifact

_REPO = Path(__file__).resolve().parents[4]
_SANDBOX_STAGING = _REPO / "build" / "chainlit" / "src" / "sandbox"
_ROOTFS_IMAGE = _SANDBOX_STAGING / "plugins" / "boba-tool-shell" / "rootfs.ext4"

_CGROUP_BASE = os.environ.get("BOBA_CGROUP_BASE", "/sys/fs/cgroup/boba")


def _cgroup_delegated() -> bool:
    base_ok = os.access(os.path.join(_CGROUP_BASE, "cgroup.procs"), os.W_OK)
    root_ok = os.access("/sys/fs/cgroup/cgroup.procs", os.W_OK)
    return base_ok and root_ok


pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(
        shutil.which("bwrap") is None or not _ROOTFS_IMAGE.exists(),
        reason="нет bwrap или артефактов песочницы (собрать: make fetch sandbox)",
    ),
    pytest.mark.skipif(
        not _cgroup_delegated(),
        reason=f"cgroup base {_CGROUP_BASE} не делегирован пользователю",
    ),
]

STAND = Stand.required()

PROFILE = "general"
"""Профиль конфига, которому выданы все инструменты."""

THREAD_ID = "55555555-5555-4555-8555-555555555555"
"""Тред хода: область describer — uuid треда."""

USER_ID = UUID("66666666-6666-4666-8666-666666666666")
LOGIN = "describer-flow"

CONNECTIONS_SCHEMA = "connections_describer"
DESCRIBER_SCHEMA = "describer_e2e"

PG_CONNECTION = "dwh"
CH_CONNECTION = "logs"

DM = "dm"
CH_DATABASE = "describer_e2e"

THREAD = RunnableConfig(configurable={"thread_id": THREAD_ID})

FINAL_ANSWER = "the schema is described and linked"

WINDOW: dict[str, int] = {"offset": 0, "max_rows": 50, "max_chars": 20000}
"""Окно выдачи каталожных инструментов: его задаёт вызов."""

FIRST_EDGE_ID = 1
LAST_NODE_ID = 6
"""id на удаление: схема пересоздаётся на тест, sequence начинает с 1; шесть
узлов и три ребра пишутся параллельно, поэтому известны только множества."""


class CallId:
    """Идентификаторы вызовов сценария: по ним ищутся ответы в истории."""

    CONNECTIONS = "call-connections"
    PG_DESCRIBE = "call-pg-describe"
    PG_FK = "call-pg-fk"
    CH_DESCRIBE = "call-ch-describe"
    PG_ADDRESS = "call-pg-address"
    CH_ADDRESS = "call-ch-address"
    NODE_ORDERS = "call-node-orders"
    NODE_ORDERS_CUSTOMER = "call-node-orders-customer"
    NODE_CUSTOMERS = "call-node-customers"
    NODE_CUSTOMERS_ID = "call-node-customers-id"
    NODE_EVENTS_USER = "call-node-events-user"
    NODE_ENTITY = "call-node-entity"
    EDGE_FK = "call-edge-fk"
    EDGE_IMPLICIT = "call-edge-implicit"
    EDGE_SIMILAR = "call-edge-similar"
    BAD_ADDRESS = "call-bad-address"
    BAD_EDGE = "call-bad-edge"
    BAD_ARGS = "call-bad-args"
    LIST_NODES = "call-list-nodes"
    LIST_EDGES = "call-list-edges"
    DELETE_EDGE = "call-delete-edge"
    DELETE_NODE = "call-delete-node"
    BAD_DELETE = "call-bad-delete"
    LIST_NODES_AFTER = "call-list-nodes-after"
    LIST_EDGES_AFTER = "call-list-edges-after"


class ScriptedChat(GenericFakeChatModel):
    """Модель по сценарию: bind_tools у фейка не реализован."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


class StoreHolder:
    """Ссылка на хранилище соединений для загрузчика: реестр живёт на модуль,
    хранилище пересоздаётся на тест."""

    store: ClassVar[ConnectionStore | None] = None

    @classmethod
    def current(cls) -> ConnectionStore:
        if cls.store is None:
            raise AssertionError("the connection store is not set up for the test")

        return cls.store


class Expected:
    """Адреса сценария, собранные моделями адресов из профилей соединений."""

    def __init__(self, pg: PostgresConfig, ch: ClickHouseConfig) -> None:
        self.pg_base = PgAddresses.base_of(pg)
        self.ch_base = ChAddresses.base_of(ch)

    def pg_table(self, table: str) -> str:
        base = self.pg_base
        return PgTableAddress(
            host=base.host,
            port=base.port,
            database=base.database,
            schema=DM,
            table=table,
        ).render()

    def pg_column(self, table: str, column: str) -> str:
        base = self.pg_base
        return PgTableColumnAddress(
            host=base.host,
            port=base.port,
            database=base.database,
            schema=DM,
            table=table,
            column=column,
        ).render()

    def ch_column(self, table: str, column: str) -> str:
        base = self.ch_base
        return ChTableColumnAddress(
            host=base.host,
            port=base.port,
            database=CH_DATABASE,
            table=table,
            column=column,
        ).render()

    @staticmethod
    def entity(name: str) -> str:
        return EntityAddress(name=name).render()


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(std_secrets.token_bytes(32)).decode())


def _call(call_id: str, name: str, **args: Any) -> dict[str, Any]:
    args["intent"] = f"scripted {name}"
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


@pytest.fixture(scope="module")
def app_config() -> AppConfig:
    """Конфиг приложения чата: профили, роли, лимиты истории."""
    return bind(AppLayers.compose(ConfigLocator.path()), path="app", model=AppConfig)


@pytest.fixture(scope="module")
def app_sandbox() -> Iterator[None]:
    """Зиготы секций гасятся после модуля, как это делает выход приложения."""
    try:
        yield
    finally:
        ZygoteRegistry.stop_all()


@pytest.fixture(scope="module")
def flow_raw(raw_config: DictConfig, test_database: str) -> DictConfig:
    """Конфиг хода: сервисный postgres смотрит в тестовую базу, чтобы и таблицы
    describer, и описываемые таблицы жили там же."""
    raw = raw_config.copy()
    OmegaConf.update(raw, "postgres.dbname", test_database)
    OmegaConf.update(raw, "tool.connections.db_schema", CONNECTIONS_SCHEMA)
    OmegaConf.update(raw, "tool.describer.db_schema", DESCRIBER_SCHEMA)
    return raw


@pytest.fixture(scope="module")
def session_tools(
    flow_raw: DictConfig, app_config: AppConfig, app_sandbox: None
) -> list[BaseTool]:
    """Инструменты профиля, собранные боевым загрузчиком над хранилищем стенда."""
    refs = StandRefs.of(StoreHolder.current, lambda: None)
    registry = ChatPlugins.load(flow_raw, refs)
    roles = frozenset(app_config.roles)
    return registry.for_session(roles, PROFILE)


@pytest.fixture
def pg_profile(flow_raw: DictConfig) -> PostgresConfig:
    """Соединение пользователя в тестовую базу: сервисный профиль с её именем."""
    service = bind(flow_raw, path="postgres", model=PostgresConfig)
    return service.model_copy(update={"description": "warehouse with dm schema"})


@pytest.fixture
def ch_profile() -> ClickHouseConfig:
    """Соединение пользователя в ClickHouse стенда парольным пользователем."""
    if not STAND.ch_user:
        pytest.skip("стенд без парольного пользователя clickhouse")

    return ClickHouseConfig.model_validate(
        {
            "host": STAND.ch_addr,
            "port": STAND.ch_port,
            "interface": "http",
            "database": STAND.ch_database,
            "connect_timeout": 10,
            "description": "event logs",
            "auth": ChPasswordAuth(
                method="password", user=STAND.ch_user, password=STAND.ch_password
            ),
        }
    )


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> AsyncIterator[ConnectionStore]:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(
                sql.Identifier(CONNECTIONS_SCHEMA)
            )
        )

    cfg = ConnectionsConfig(
        enable=True, db_schema=CONNECTIONS_SCHEMA, encryption_key=_key()
    )
    built = ConnectionStore(cfg, ConnectionTypes.discover(), pool)
    await built.setup()
    StoreHolder.store = built
    try:
        yield built
    finally:
        StoreHolder.store = None


@pytest.fixture
async def granted(
    store: ConnectionStore, pg_profile: PostgresConfig, ch_profile: ClickHouseConfig
) -> None:
    """Оба соединения выданы пользователю хода лично."""
    pg_id = await store.add(PG_CONNECTION, pg_profile)
    await store.grant(pg_id, GrantTarget.user(USER_ID))

    ch_id = await store.add(CH_CONNECTION, ch_profile)
    await store.grant(ch_id, GrantTarget.user(USER_ID))


@pytest.fixture
async def seeded_pg(pool: AsyncPostgresPool) -> None:
    """Описываемые таблицы: customers и orders с внешним ключом; таблицы
    describer сброшены."""
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(
                sql.Identifier(DESCRIBER_SCHEMA)
            )
        )
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(DM))
        )
        await conn.execute(sql.SQL("create schema {}").format(sql.Identifier(DM)))
        await conn.execute(
            sql.SQL(
                "create table {} (id bigint primary key, name text not null)"
            ).format(sql.Identifier(DM, "customers"))
        )
        await conn.execute(
            sql.SQL(
                "create table {orders} (id bigint primary key, "
                "customer_id bigint not null references {customers}, "
                "amount numeric not null)"
            ).format(
                orders=sql.Identifier(DM, "orders"),
                customers=sql.Identifier(DM, "customers"),
            )
        )


@pytest.fixture
async def seeded_ch(ch_profile: ClickHouseConfig) -> AsyncIterator[None]:
    """Таблица событий в ClickHouse стенда: её колонку свяжем с customers.id;
    база стенда после теста убирается."""
    async with PayloadClickHouse.opened_config(ch_profile) as client:
        await client.command(f"create database if not exists {CH_DATABASE}")
        await client.command(f"drop table if exists {CH_DATABASE}.events")
        await client.command(
            f"create table {CH_DATABASE}.events "
            "(user_id UInt64, ts DateTime) engine = MergeTree order by ts"
        )

    yield

    async with PayloadClickHouse.opened_config(ch_profile) as client:
        await client.command(f"drop database if exists {CH_DATABASE}")


@pytest.fixture
async def chainlit_context(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сессия пользователя хода: его id совпадает с целью грантов."""
    from chainlit.context import init_http_context

    roles = sorted(app_config.roles)
    user = cl.User(identifier=LOGIN, metadata={"roles": roles})

    context = init_http_context(user=user)
    context.session.chat_profile = PROFILE
    use_context(
        monkeypatch,
        thread_id=THREAD_ID,
        user_id=USER_ID,
        roles=roles,
        profile=PROFILE,
        login=LOGIN,
    )


def _graph(
    app_config: AppConfig,
    tools: Sequence[BaseTool],
    scripted: Sequence[AIMessage],
) -> CompiledStateGraph:
    """Граф профиля на модели по сценарию: боевой билдер, память вместо postgres."""
    settings = app_config.profiles[PROFILE]

    chat = ScriptedChat(messages=iter(list(scripted)), disable_streaming=True)

    names: list[str] = []
    for tool in tools:
        names.append(tool.name)

    spec = GraphSpec(
        chat=chat,
        tools=tools,
        system_prompt=settings.system_prompt,
        checkpointer=InMemorySaver(),
        history=build_history_view(frozenset(names), settings.history_messages),
    )

    return PlainGraphBuilder().build(spec)


def _script(expected: Expected) -> list[AIMessage]:
    """Сценарий агента: разведка метаданных, адреса, узлы, рёбра, ошибки, список."""
    orders = expected.pg_table("orders")
    orders_customer = expected.pg_column("orders", "customer_id")
    customers = expected.pg_table("customers")
    customers_id = expected.pg_column("customers", "id")
    events_user = expected.ch_column("events", "user_id")
    customer = expected.entity("customer")

    fk_sql = (
        "select conname, pg_get_constraintdef(oid) as definition "
        "from pg_constraint where contype = 'f' "
        "and conrelid = 'dm.orders'::regclass"
    )

    return [
        AIMessage(
            content="", tool_calls=[_call(CallId.CONNECTIONS, "connection_list")]
        ),
        AIMessage(
            content="",
            tool_calls=[
                _call(
                    CallId.PG_DESCRIBE,
                    "pg_describe_table",
                    connection=PG_CONNECTION,
                    table="orders",
                    pg_schema=DM,
                    **WINDOW,
                ),
                _call(CallId.PG_FK, "pg_query", connection=PG_CONNECTION, sql=fk_sql),
                _call(
                    CallId.CH_DESCRIBE,
                    "ch_describe_table",
                    connection=CH_CONNECTION,
                    table="events",
                    ch_database=CH_DATABASE,
                    **WINDOW,
                ),
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                _call(CallId.PG_ADDRESS, "pg_address", connection=PG_CONNECTION),
                _call(CallId.CH_ADDRESS, "ch_address", connection=CH_CONNECTION),
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                _call(
                    CallId.NODE_ORDERS,
                    "describe_node",
                    kind="pg_table",
                    address=orders,
                    description="orders placed by customers",
                ),
                _call(
                    CallId.NODE_ORDERS_CUSTOMER,
                    "describe_node",
                    kind="pg_column",
                    address=orders_customer,
                    description="customer who placed the order",
                ),
                _call(
                    CallId.NODE_CUSTOMERS,
                    "describe_node",
                    kind="pg_table",
                    address=customers,
                    description="registered customers",
                ),
                _call(
                    CallId.NODE_CUSTOMERS_ID,
                    "describe_node",
                    kind="pg_column",
                    address=customers_id,
                    description="customer surrogate key",
                ),
                _call(
                    CallId.NODE_EVENTS_USER,
                    "describe_node",
                    kind="ch_column",
                    address=events_user,
                    description="customer id in the event stream",
                ),
                _call(
                    CallId.NODE_ENTITY,
                    "describe_node",
                    kind="entity",
                    address=customer,
                    description="a person who buys",
                ),
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                _call(
                    CallId.EDGE_FK,
                    "describe_edge",
                    source=orders_customer,
                    target=customers_id,
                    kind="foreign_key",
                    description="orders_customer_id_fkey",
                ),
                _call(
                    CallId.EDGE_IMPLICIT,
                    "describe_edge",
                    source=events_user,
                    target=customers_id,
                    kind="implicit_key",
                    description="event user ids are customer ids",
                ),
                _call(
                    CallId.EDGE_SIMILAR,
                    "describe_edge",
                    source=customers,
                    target=customer,
                    kind="similar",
                    description="the table holds customers",
                ),
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                _call(
                    CallId.BAD_ADDRESS,
                    "describe_node",
                    kind="pg_table",
                    address=orders_customer,
                    description="kind and address disagree",
                ),
                _call(
                    CallId.BAD_EDGE,
                    "describe_edge",
                    source=expected.pg_table("payments"),
                    target=customers,
                    kind="similar",
                    description="payments were never described",
                ),
                _call(
                    CallId.BAD_ARGS,
                    "describe_node",
                    kind="pg_table",
                    address=orders,
                ),
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                _call(CallId.LIST_NODES, "describe_list_nodes"),
                _call(CallId.LIST_EDGES, "describe_list_edges"),
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                _call(CallId.DELETE_EDGE, "describe_delete_edge", ids=[FIRST_EDGE_ID]),
                _call(CallId.DELETE_NODE, "describe_delete_node", ids=[LAST_NODE_ID]),
                _call(CallId.BAD_DELETE, "describe_delete_node", ids=[999_999]),
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                _call(CallId.LIST_NODES_AFTER, "describe_list_nodes"),
                _call(CallId.LIST_EDGES_AFTER, "describe_list_edges"),
            ],
        ),
        AIMessage(content=FINAL_ANSWER),
    ]


def node_calls_urls(expected: Expected) -> list[str]:
    """Все url узлов сценария в порядке вызовов."""
    return [
        expected.pg_table("orders"),
        expected.pg_column("orders", "customer_id"),
        expected.pg_table("customers"),
        expected.pg_column("customers", "id"),
        expected.ch_column("events", "user_id"),
        expected.entity("customer"),
    ]


def _replies(messages: Sequence[BaseMessage]) -> dict[str, ToolMessage]:
    by_call: dict[str, ToolMessage] = {}
    for message in messages:
        if isinstance(message, ToolMessage):
            by_call[message.tool_call_id] = message

    return by_call


class Replies:
    """Ответы инструментов хода по id вызова."""

    def __init__(self, messages: Sequence[BaseMessage]) -> None:
        self._by_call = _replies(messages)

    def ok(self, call_id: str) -> Any:
        reply = self._reply(call_id)
        if reply.status == "error":
            raise AssertionError(f"{call_id} failed: {reply.content}")

        artifact = ToolArtifact.revive(reply.artifact)
        if artifact is None:
            raise AssertionError(f"{call_id}: artifact is not revived")

        if isinstance(artifact, ErrorResult):
            raise AssertionError(f"{call_id} failed: {artifact.message}")

        return artifact

    def refused(self, call_id: str) -> ErrorResult:
        """Отказ тела: сообщение со статусом хода и артефактом ErrorResult."""
        reply = self._reply(call_id)
        artifact = ToolArtifact.revive(reply.artifact)
        if not isinstance(artifact, ErrorResult):
            raise AssertionError(f"{call_id} must be refused, got {reply.content!r}")

        return artifact

    def invalid(self, call_id: str) -> ToolMessage:
        """Отказ валидации аргументов до тела: статус error у сообщения."""
        reply = self._reply(call_id)
        if reply.status != "error":
            raise AssertionError(
                f"{call_id} must fail validation, got {reply.content!r}"
            )

        return reply

    def _reply(self, call_id: str) -> ToolMessage:
        reply = self._by_call.get(call_id)
        if reply is None:
            raise AssertionError(f"history has no tool message for {call_id}")

        return reply


def _rows(artifact: Any) -> list[dict[str, Any]]:
    if isinstance(artifact, SqlResult):
        rows: list[dict[str, Any]] = []
        for statement in artifact.statements:
            if statement.rows is None:
                continue

            for row in statement.rows:
                rows.append(dict(row))

        return rows

    if isinstance(artifact, TableResult):
        table: list[dict[str, Any]] = []
        for row in artifact.rows:
            table.append(dict(row))

        return table

    raise AssertionError(f"rows expected from SqlResult or TableResult, got {artifact}")


def _column(rows: Sequence[Mapping[str, Any]], name: str) -> list[Any]:
    values: list[Any] = []
    for row in rows:
        values.append(row[name])

    return values


async def _stored_nodes(pool: AsyncPostgresPool) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            sql.SQL(
                "select scope_kind, scope_id::text, kind, address, url_address, "
                "description from {} order by id"
            ).format(sql.Identifier(DESCRIBER_SCHEMA, "node"))
        )
        rows = await cur.fetchall()

    nodes: list[dict[str, Any]] = []
    for row in rows:
        nodes.append(
            {
                "scope_kind": row[0],
                "scope_id": row[1],
                "kind": row[2],
                "address": row[3],
                "url": row[4],
                "description": row[5],
            }
        )

    return nodes


async def _stored_edges(pool: AsyncPostgresPool) -> list[tuple[str, str, str]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            sql.SQL(
                "select s.url_address, t.url_address, e.kind from {edge} e "
                "join {node} s on s.id = e.source_id "
                "join {node} t on t.id = e.target_id order by e.id"
            ).format(
                edge=sql.Identifier(DESCRIBER_SCHEMA, "edge"),
                node=sql.Identifier(DESCRIBER_SCHEMA, "node"),
            )
        )
        rows = await cur.fetchall()

    edges: list[tuple[str, str, str]] = []
    for row in rows:
        edges.append((str(row[0]), str(row[1]), str(row[2])))

    return edges


@pytest.mark.usefixtures("chainlit_context", "granted", "seeded_pg", "seeded_ch")
async def test_agent_describes_schema_and_links(  # noqa: PLR0915 — один ход, много проверок
    app_config: AppConfig,
    session_tools: list[BaseTool],
    pg_profile: PostgresConfig,
    ch_profile: ClickHouseConfig,
    pool: AsyncPostgresPool,
) -> None:
    expected = Expected(pg_profile, ch_profile)
    graph = _graph(app_config, session_tools, _script(expected))

    result = await graph.ainvoke(
        {"messages": [HumanMessage("describe the dm schema and its links")]},
        config=THREAD,
    )
    messages = result["messages"]
    replies = Replies(messages)

    # разведка: оба соединения видны, метаданные приходят из настоящих баз
    names = _column(_rows(replies.ok(CallId.CONNECTIONS)), "connection")
    assert sorted(names) == [PG_CONNECTION, CH_CONNECTION]

    columns = _column(_rows(replies.ok(CallId.PG_DESCRIBE)), "column_name")
    assert columns == ["id", "customer_id", "amount"]

    constraints = _rows(replies.ok(CallId.PG_FK))
    assert len(constraints) == 1
    assert "customers" in str(constraints[0]["definition"])

    ch_columns = _column(_rows(replies.ok(CallId.CH_DESCRIBE)), "name")
    assert ch_columns == ["user_id", "ts"]

    # базовые url соединений совпадают с рендером моделей адресов
    pg_address = _rows(replies.ok(CallId.PG_ADDRESS))[0]
    assert pg_address["connection"] == PG_CONNECTION
    assert pg_address["url"] == expected.pg_base.render()

    ch_address = _rows(replies.ok(CallId.CH_ADDRESS))[0]
    assert ch_address["url"] == expected.ch_base.render()

    # узлы записаны, ответ несёт канонический url и вид
    node_calls = {
        CallId.NODE_ORDERS: ("pg_table", expected.pg_table("orders")),
        CallId.NODE_ORDERS_CUSTOMER: (
            "pg_column",
            expected.pg_column("orders", "customer_id"),
        ),
        CallId.NODE_CUSTOMERS: ("pg_table", expected.pg_table("customers")),
        CallId.NODE_CUSTOMERS_ID: ("pg_column", expected.pg_column("customers", "id")),
        CallId.NODE_EVENTS_USER: ("ch_column", expected.ch_column("events", "user_id")),
        CallId.NODE_ENTITY: ("entity", expected.entity("customer")),
    }
    for call_id, (kind, url) in node_calls.items():
        row = _rows(replies.ok(call_id))[0]
        assert row["kind"] == kind, call_id
        assert row["url"] == url, call_id
        assert row["action"] == "inserted", call_id

    # рёбра записаны, в том числе кроссбазное и на понятие
    for call_id in (CallId.EDGE_FK, CallId.EDGE_IMPLICIT, CallId.EDGE_SIMILAR):
        row = _rows(replies.ok(call_id))[0]
        assert row["action"] == "inserted", call_id

    # ошибочные вызовы отвечают отказом, ход продолжается
    bad_address = replies.refused(CallId.BAD_ADDRESS)
    assert bad_address.error_kind == "invalid_address"
    assert "pg_table" in bad_address.message

    bad_edge = replies.refused(CallId.BAD_EDGE)
    assert bad_edge.error_kind == "node_missing"
    assert expected.pg_table("payments") in bad_edge.message

    bad_args = replies.invalid(CallId.BAD_ARGS)
    assert "description" in str(bad_args.content)
    assert "required" in str(bad_args.content).lower()

    # списки области: шесть узлов и три ребра со своими id
    nodes_listing = _rows(replies.ok(CallId.LIST_NODES))
    edges_listing = _rows(replies.ok(CallId.LIST_EDGES))
    assert sorted(_column(nodes_listing, NodeListColumn.ID.value)) == list(range(1, 7))
    assert sorted(_column(edges_listing, EdgeListColumn.ID.value)) == [1, 2, 3]

    # удаление по id: ребро и узел с его рёбрами сняты, чужой id отвергнут
    deleted_edge = _rows(replies.ok(CallId.DELETE_EDGE))
    assert deleted_edge == [{"id": FIRST_EDGE_ID, "action": "deleted"}]

    deleted_node = _rows(replies.ok(CallId.DELETE_NODE))
    assert deleted_node == [{"id": LAST_NODE_ID, "action": "deleted"}]

    bad_delete = replies.refused(CallId.BAD_DELETE)
    assert bad_delete.error_kind == "node_id_missing"
    assert "999999" in bad_delete.message

    nodes_after = _rows(replies.ok(CallId.LIST_NODES_AFTER))
    edges_after = _rows(replies.ok(CallId.LIST_EDGES_AFTER))
    assert len(nodes_after) == 5
    assert FIRST_EDGE_ID not in _column(edges_after, EdgeListColumn.ID.value)

    last = messages[-1]
    assert isinstance(last, AIMessage)
    assert last.content == FINAL_ANSWER

    # таблицы до удаления проверялись ответами; после — строки области треда
    nodes = await _stored_nodes(pool)
    assert len(nodes) == 5
    for node in nodes:
        assert node["scope_kind"] == "chat"
        assert node["scope_id"] == THREAD_ID

    by_url = {node["url"]: node for node in nodes}
    deleted_urls = set(node_calls_urls(expected)) - set(by_url)
    assert len(deleted_urls) == 1

    for url, node in by_url.items():
        address = Addresses.parse_any(url)
        assert node["address"] == address.to_json(), url
        assert node["kind"] == type(address).KIND, url

    # рёбра: ни одно не ссылается на снятый узел, снятого ребра нет
    edges = await _stored_edges(pool)
    for source, target, _ in edges:
        assert source not in deleted_urls
        assert target not in deleted_urls

    expected_edges = {
        (
            expected.pg_column("orders", "customer_id"),
            expected.pg_column("customers", "id"),
            EdgeKind.FOREIGN_KEY.value,
        ),
        (
            expected.ch_column("events", "user_id"),
            expected.pg_column("customers", "id"),
            EdgeKind.IMPLICIT_KEY.value,
        ),
        (
            expected.pg_table("customers"),
            expected.entity("customer"),
            EdgeKind.SIMILAR.value,
        ),
    }
    assert set(edges) < expected_edges


@pytest.mark.usefixtures("chainlit_context", "granted", "seeded_pg", "seeded_ch")
async def test_second_turn_updates_instead_of_duplicating(
    app_config: AppConfig,
    session_tools: list[BaseTool],
    pg_profile: PostgresConfig,
    ch_profile: ClickHouseConfig,
    pool: AsyncPostgresPool,
) -> None:
    """Повторный ход того же треда переописывает объект: строка одна, текст новый."""
    expected = Expected(pg_profile, ch_profile)
    orders = expected.pg_table("orders")

    first = [
        AIMessage(
            content="",
            tool_calls=[
                _call(
                    CallId.NODE_ORDERS,
                    "describe_node",
                    kind="pg_table",
                    address=orders,
                    description="first take",
                )
            ],
        ),
        AIMessage(content=FINAL_ANSWER),
    ]
    await _graph(app_config, session_tools, first).ainvoke(
        {"messages": [HumanMessage("describe orders")]}, config=THREAD
    )

    second = [
        AIMessage(
            content="",
            tool_calls=[
                _call(
                    "call-node-orders-again",
                    "describe_node",
                    kind="pg_table",
                    address=orders,
                    description="second take",
                )
            ],
        ),
        AIMessage(content=FINAL_ANSWER),
    ]
    result = await _graph(app_config, session_tools, second).ainvoke(
        {"messages": [HumanMessage("describe orders better")]}, config=THREAD
    )

    row = _rows(Replies(result["messages"]).ok("call-node-orders-again"))[0]
    assert row["action"] == "updated"

    nodes = await _stored_nodes(pool)
    assert len(nodes) == 1
    assert nodes[0]["description"] == "second take"
