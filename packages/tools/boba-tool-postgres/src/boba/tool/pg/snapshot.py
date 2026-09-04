"""Снятие структуры базы Postgres в кадры синхронизации каталога.

Инструмент pg_schema_snapshot читает системные каталоги подключения
пользователя порциями с паузами и шлёт в выходной порт кадры SyncPlan,
SyncBatch и SyncDone (boba.catalog.sync); записи — модели PgSnapshot
каталога. База одна — та, к которой подключение: чужие базы Postgres не
показывает. Хост складывает порции в staging и по итогу собирает версию
источника.

Ошибки:
PostgresError — до базы не достучаться (сеть, отказ libpq, kerberos).
psycopg.Error — сервер отклонил каталожный запрос (права на каталог).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator, Mapping, Sequence
from enum import IntEnum, StrEnum
from typing import Annotated, Any, ClassVar, Final

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from boba.catalog import SourceRecord, SyncEmitter, SyncFrame, SyncPlan
from boba.db.postgres import PayloadPostgres
from boba.db.postgres.profile import PostgresConfig
from boba.db.postgres.snapshot import (
    PgColumn,
    PgConstraint,
    PgConstraintKind,
    PgDatabase,
    PgIndex,
    PgPart,
    PgRelation,
    PgRelationKind,
    PgRoutine,
    PgRoutineArg,
    PgRoutineKind,
    PgSchema,
    PgSequence,
    PgSnapshot,
    PgSourceKind,
    PgType,
    PgTypeAttribute,
    PgTypeKind,
)
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, UserConnection, tool
from boba.toolkit.ports import Outbound
from boba.toolkit.result import TextResult, ToolResult, pack_result

PgConnection = Annotated[PostgresConfig, UserConnection]


class SystemSchema(StrEnum):
    """Схемы, которые без явного списка не снимаются."""

    PG_CATALOG = "pg_catalog"
    INFORMATION_SCHEMA = "information_schema"
    PG_TOAST = "pg_toast"

    TEMP_PREFIX = "pg_temp_"
    TOAST_TEMP_PREFIX = "pg_toast_temp_"

    @classmethod
    def names(cls) -> tuple[str, ...]:
        return (cls.PG_CATALOG.value, cls.INFORMATION_SCHEMA.value, cls.PG_TOAST.value)


class Batching(IntEnum):
    """Пределы порций: размер и пауза задаются вызовом в этих границах."""

    MIN_BATCH = 1
    MAX_BATCH = 1000
    MIN_PAUSE_MS = 0
    MAX_PAUSE_MS = 60_000
    MS_PER_SECOND = 1000


class Relkind(StrEnum):
    """relkind pg_class, которые входят в снимок."""

    TABLE = "r"
    PARTITIONED = "p"
    VIEW = "v"
    MATERIALIZED = "m"
    FOREIGN = "f"

    @classmethod
    def values(cls) -> list[str]:
        return [kind.value for kind in cls]

    def relation_kind(self, *, is_partition: bool) -> PgRelationKind:
        if self is Relkind.TABLE and is_partition:
            return PgRelationKind.PARTITION

        return _RELATION_KINDS[self]


_RELATION_KINDS: Mapping[Relkind, PgRelationKind] = {
    Relkind.TABLE: PgRelationKind.TABLE,
    Relkind.PARTITIONED: PgRelationKind.PARTITIONED,
    Relkind.VIEW: PgRelationKind.VIEW,
    Relkind.MATERIALIZED: PgRelationKind.MATERIALIZED,
    Relkind.FOREIGN: PgRelationKind.FOREIGN,
}


class Persistence(StrEnum):
    PERMANENT = "p"
    UNLOGGED = "u"
    TEMPORARY = "t"

    def label(self) -> str:
        return self.name.lower()


class Contype(StrEnum):
    PRIMARY = "p"
    UNIQUE = "u"
    FOREIGN = "f"
    CHECK = "c"
    EXCLUSION = "x"

    def constraint_kind(self) -> PgConstraintKind:
        return _CONSTRAINT_KINDS[self]

    @classmethod
    def values(cls) -> list[str]:
        return [kind.value for kind in cls]


_CONSTRAINT_KINDS: Mapping[Contype, PgConstraintKind] = {
    Contype.PRIMARY: PgConstraintKind.PRIMARY,
    Contype.UNIQUE: PgConstraintKind.UNIQUE,
    Contype.FOREIGN: PgConstraintKind.FOREIGN,
    Contype.CHECK: PgConstraintKind.CHECK,
    Contype.EXCLUSION: PgConstraintKind.EXCLUSION,
}


class ForeignAction(StrEnum):
    """confupdtype и confdeltype pg_constraint словами."""

    NO_ACTION = "a"
    RESTRICT = "r"
    CASCADE = "c"
    SET_NULL = "n"
    SET_DEFAULT = "d"

    def label(self) -> str:
        return self.name.lower().replace("_", " ")


class Prokind(StrEnum):
    FUNCTION = "f"
    PROCEDURE = "p"
    AGGREGATE = "a"
    WINDOW = "w"

    def routine_kind(self) -> PgRoutineKind:
        return _ROUTINE_KINDS[self]


_ROUTINE_KINDS: Mapping[Prokind, PgRoutineKind] = {
    Prokind.FUNCTION: PgRoutineKind.FUNCTION,
    Prokind.PROCEDURE: PgRoutineKind.PROCEDURE,
    Prokind.AGGREGATE: PgRoutineKind.AGGREGATE,
    Prokind.WINDOW: PgRoutineKind.WINDOW,
}


class Volatility(StrEnum):
    IMMUTABLE = "i"
    STABLE = "s"
    VOLATILE = "v"

    def label(self) -> str:
        return self.name.lower()


class Parallel(StrEnum):
    SAFE = "s"
    RESTRICTED = "r"
    UNSAFE = "u"

    def label(self) -> str:
        return self.name.lower()


class Argmode(StrEnum):
    IN = "i"
    OUT = "o"
    INOUT = "b"
    VARIADIC = "v"
    TABLE = "t"

    def label(self) -> str:
        return self.name.lower()


class Typtype(StrEnum):
    ENUM = "e"
    DOMAIN = "d"
    COMPOSITE = "c"
    RANGE = "r"

    def type_kind(self) -> PgTypeKind:
        return _TYPE_KINDS[self]

    @classmethod
    def values(cls) -> list[str]:
        return [kind.value for kind in cls]


_TYPE_KINDS: Mapping[Typtype, PgTypeKind] = {
    Typtype.ENUM: PgTypeKind.ENUM,
    Typtype.DOMAIN: PgTypeKind.DOMAIN,
    Typtype.COMPOSITE: PgTypeKind.COMPOSITE,
    Typtype.RANGE: PgTypeKind.RANGE,
}


class Identity(StrEnum):
    ALWAYS = "a"
    BY_DEFAULT = "d"

    def label(self) -> str:
        return self.name.lower().replace("_", " ")


class Generated(StrEnum):
    STORED = "s"

    def label(self) -> str:
        return self.name.lower()


class SnapshotSql:
    """Каталожные запросы снятия: значения параметрами, порции по oid."""

    DATABASE: ClassVar[sql.SQL] = sql.SQL("""
        select
            d.datname                                       as name,
            pg_catalog.pg_get_userbyid(d.datdba)            as owner,
            pg_catalog.pg_encoding_to_char(d.encoding)      as encoding,
            d.datcollate                                    as collate,
            pg_catalog.shobj_description(d.oid, 'pg_database') as comment,
            current_setting('server_version')               as server_version
        from pg_catalog.pg_database d
        where d.datname = current_database()
    """)

    SCHEMAS: ClassVar[sql.SQL] = sql.SQL("""
        select
            n.nspname                                        as name,
            pg_catalog.pg_get_userbyid(n.nspowner)           as owner,
            pg_catalog.obj_description(n.oid, 'pg_namespace') as comment
        from pg_catalog.pg_namespace n
        where n.nspname = any(%(schemas)s)
        order by n.nspname
    """)

    USER_SCHEMAS: ClassVar[sql.SQL] = sql.SQL("""
        select n.nspname
        from pg_catalog.pg_namespace n
        where n.nspname <> all(%(system)s)
          and n.nspname not like %(temp)s
          and n.nspname not like %(toast_temp)s
        order by n.nspname
    """)

    COUNTS: ClassVar[sql.SQL] = sql.SQL("""
        select
            (select count(*) from pg_catalog.pg_class c
                join pg_catalog.pg_namespace n on n.oid = c.relnamespace
                where n.nspname = any(%(schemas)s)
                  and c.relkind = any(%(relkinds)s))          as relations,
            (select count(*) from pg_catalog.pg_proc p
                join pg_catalog.pg_namespace n on n.oid = p.pronamespace
                where n.nspname = any(%(schemas)s))            as routines,
            (select count(*) from pg_catalog.pg_class c
                join pg_catalog.pg_namespace n on n.oid = c.relnamespace
                where n.nspname = any(%(schemas)s)
                  and c.relkind = 'S')                         as sequences,
            (select count(*) from pg_catalog.pg_type t
                join pg_catalog.pg_namespace n on n.oid = t.typnamespace
                left join pg_catalog.pg_class c on c.oid = t.typrelid
                where n.nspname = any(%(schemas)s)
                  and t.typtype = any(%(typtypes)s)
                  and (t.typtype <> 'c' or c.relkind = 'c'))   as types
    """)

    RELATIONS: ClassVar[sql.SQL] = sql.SQL("""
        select
            c.oid                                              as oid,
            n.nspname                                          as schema_name,
            c.relname                                          as name,
            c.relkind                                          as relkind,
            c.relispartition                                   as is_partition,
            pg_catalog.pg_get_userbyid(c.relowner)             as owner,
            pg_catalog.obj_description(c.oid, 'pg_class')      as comment,
            ts.spcname                                         as tablespace,
            c.relpersistence                                   as persistence,
            greatest(c.reltuples, 0)::bigint                   as row_estimate,
            pg_catalog.pg_total_relation_size(c.oid)           as total_bytes,
            case when c.relkind = 'p'
                 then pg_catalog.pg_get_partkeydef(c.oid) end  as partition_key,
            case when parent.oid is not null
                 then pn.nspname || '.' || parent.relname end  as partition_of,
            pg_catalog.pg_get_expr(c.relpartbound, c.oid)      as partition_bound,
            case when c.relkind in ('v', 'm')
                 then pg_catalog.pg_get_viewdef(c.oid, true) end as definition,
            case when c.relkind = 'm' then c.relispopulated end as populated,
            fs.srvname                                         as foreign_server,
            c.reloptions                                       as options
        from pg_catalog.pg_class c
            join pg_catalog.pg_namespace n on n.oid = c.relnamespace
            left join pg_catalog.pg_tablespace ts on ts.oid = c.reltablespace
            left join pg_catalog.pg_inherits inh on inh.inhrelid = c.oid
            left join pg_catalog.pg_class parent
                on parent.oid = inh.inhparent and c.relispartition
            left join pg_catalog.pg_namespace pn on pn.oid = parent.relnamespace
            left join pg_catalog.pg_foreign_table ft on ft.ftrelid = c.oid
            left join pg_catalog.pg_foreign_server fs on fs.oid = ft.ftserver
        where n.nspname = any(%(schemas)s)
          and c.relkind = any(%(relkinds)s)
          and c.oid > %(after)s
        order by c.oid
        limit %(limit)s
    """)

    COLUMNS: ClassVar[sql.SQL] = sql.SQL("""
        select
            n.nspname                                          as schema_name,
            c.relname                                          as relation,
            a.attname                                          as name,
            a.attnum                                           as ordinal,
            pg_catalog.format_type(a.atttypid, a.atttypmod)    as type,
            not a.attnotnull                                   as nullable,
            pg_catalog.pg_get_expr(d.adbin, d.adrelid)         as "default",
            a.attidentity                                      as identity,
            a.attgenerated                                     as generated,
            case when a.attcollation <> t.typcollation
                 then co.collname end                          as collation,
            pg_catalog.col_description(a.attrelid, a.attnum)   as comment
        from pg_catalog.pg_attribute a
            join pg_catalog.pg_class c on c.oid = a.attrelid
            join pg_catalog.pg_namespace n on n.oid = c.relnamespace
            join pg_catalog.pg_type t on t.oid = a.atttypid
            left join pg_catalog.pg_attrdef d
                on d.adrelid = a.attrelid and d.adnum = a.attnum
            left join pg_catalog.pg_collation co on co.oid = a.attcollation
        where a.attrelid = any(%(oids)s)
          and a.attnum > 0
          and not a.attisdropped
        order by a.attrelid, a.attnum
    """)

    CONSTRAINTS: ClassVar[sql.SQL] = sql.SQL("""
        select
            n.nspname                                          as schema_name,
            c.relname                                          as relation,
            con.conname                                        as name,
            con.contype                                        as contype,
            array(
                select a.attname from pg_catalog.pg_attribute a
                where a.attrelid = con.conrelid and a.attnum = any(con.conkey)
                order by array_position(con.conkey, a.attnum)
            )                                                  as columns,
            rn.nspname                                         as ref_schema,
            rc.relname                                         as ref_relation,
            case when con.confrelid <> 0 then array(
                select a.attname from pg_catalog.pg_attribute a
                where a.attrelid = con.confrelid and a.attnum = any(con.confkey)
                order by array_position(con.confkey, a.attnum)
            ) end                                              as ref_columns,
            case when con.contype = 'f' then con.confupdtype end as on_update,
            case when con.contype = 'f' then con.confdeltype end as on_delete,
            con.condeferrable                                  as deferrable,
            con.condeferred                                    as initially_deferred,
            pg_catalog.pg_get_constraintdef(con.oid, true)     as definition,
            pg_catalog.obj_description(con.oid, 'pg_constraint') as comment
        from pg_catalog.pg_constraint con
            join pg_catalog.pg_class c on c.oid = con.conrelid
            join pg_catalog.pg_namespace n on n.oid = c.relnamespace
            left join pg_catalog.pg_class rc on rc.oid = con.confrelid
            left join pg_catalog.pg_namespace rn on rn.oid = rc.relnamespace
        where con.conrelid = any(%(oids)s)
          and con.contype = any(%(contypes)s)
        order by con.conrelid, con.conname
    """)

    INDEXES: ClassVar[sql.SQL] = sql.SQL("""
        select
            n.nspname                                          as schema_name,
            c.relname                                          as relation,
            ic.relname                                         as name,
            am.amname                                          as method,
            i.indisunique                                      as unique,
            i.indisprimary                                     as primary,
            array(
                select pg_catalog.pg_get_indexdef(i.indexrelid, k.n, true)
                from generate_series(1, i.indnkeyatts) as k(n)
            )                                                  as columns,
            pg_catalog.pg_get_expr(i.indpred, i.indrelid, true) as predicate,
            pg_catalog.pg_get_indexdef(i.indexrelid)           as definition,
            pg_catalog.pg_relation_size(i.indexrelid)          as total_bytes,
            pg_catalog.obj_description(i.indexrelid, 'pg_class') as comment
        from pg_catalog.pg_index i
            join pg_catalog.pg_class c on c.oid = i.indrelid
            join pg_catalog.pg_namespace n on n.oid = c.relnamespace
            join pg_catalog.pg_class ic on ic.oid = i.indexrelid
            join pg_catalog.pg_am am on am.oid = ic.relam
        where i.indrelid = any(%(oids)s)
        order by i.indrelid, ic.relname
    """)

    ROUTINES: ClassVar[sql.SQL] = sql.SQL("""
        select
            p.oid                                              as oid,
            n.nspname                                          as schema_name,
            p.proname                                          as name,
            pg_catalog.oidvectortypes(p.proargtypes)           as signature,
            p.prokind                                          as prokind,
            pg_catalog.pg_get_userbyid(p.proowner)             as owner,
            l.lanname                                          as language,
            pg_catalog.pg_get_function_arguments(p.oid)        as arguments,
            case when p.prokind <> 'p'
                 then pg_catalog.pg_get_function_result(p.oid) end as returns,
            p.proretset                                        as returns_set,
            p.provolatile                                      as volatility,
            p.proisstrict                                      as strict,
            p.prosecdef                                        as security_definer,
            p.proparallel                                      as parallel,
            p.procost                                          as cost,
            case when p.proretset then p.prorows end           as rows,
            coalesce(p.prosrc, '')                             as body,
            case when p.prokind <> 'a'
                 then pg_catalog.pg_get_functiondef(p.oid) end as definition,
            pg_catalog.obj_description(p.oid, 'pg_proc')       as comment,
            p.proargnames                                      as argnames,
            p.proargmodes                                      as argmodes,
            coalesce(p.proallargtypes, p.proargtypes::oid[])   as argtypes
        from pg_catalog.pg_proc p
            join pg_catalog.pg_namespace n on n.oid = p.pronamespace
            join pg_catalog.pg_language l on l.oid = p.prolang
        where n.nspname = any(%(schemas)s)
          and p.oid > %(after)s
        order by p.oid
        limit %(limit)s
    """)

    SEQUENCES: ClassVar[sql.SQL] = sql.SQL("""
        select
            c.oid                                              as oid,
            n.nspname                                          as schema_name,
            c.relname                                          as name,
            pg_catalog.format_type(s.seqtypid, null)           as type,
            s.seqstart                                         as start,
            s.seqmin                                           as minimum,
            s.seqmax                                           as maximum,
            s.seqincrement                                     as increment,
            s.seqcycle                                         as cycle,
            s.seqcache                                         as cache,
            (select ps.last_value from pg_catalog.pg_sequences ps
                where ps.schemaname = n.nspname
                  and ps.sequencename = c.relname)             as last_value,
            (select on_.nspname || '.' || oc.relname || '.' || oa.attname
                from pg_catalog.pg_depend dep
                join pg_catalog.pg_class oc on oc.oid = dep.refobjid
                join pg_catalog.pg_namespace on_ on on_.oid = oc.relnamespace
                join pg_catalog.pg_attribute oa
                    on oa.attrelid = dep.refobjid and oa.attnum = dep.refobjsubid
                where dep.objid = c.oid and dep.deptype = 'a'
                  and dep.classid = 'pg_class'::regclass
                limit 1)                                       as owned_by,
            pg_catalog.obj_description(c.oid, 'pg_class')      as comment
        from pg_catalog.pg_class c
            join pg_catalog.pg_namespace n on n.oid = c.relnamespace
            join pg_catalog.pg_sequence s on s.seqrelid = c.oid
        where n.nspname = any(%(schemas)s)
          and c.relkind = 'S'
          and c.oid > %(after)s
        order by c.oid
        limit %(limit)s
    """)

    TYPES: ClassVar[sql.SQL] = sql.SQL("""
        select
            t.oid                                              as oid,
            n.nspname                                          as schema_name,
            t.typname                                          as name,
            t.typtype                                          as typtype,
            pg_catalog.pg_get_userbyid(t.typowner)             as owner,
            case when t.typtype = 'e' then array(
                select e.enumlabel from pg_catalog.pg_enum e
                where e.enumtypid = t.oid order by e.enumsortorder
            ) end                                              as labels,
            case when t.typtype = 'd'
                 then pg_catalog.format_type(t.typbasetype, t.typtypmod) end
                                                               as base_type,
            case when t.typtype = 'd' then (
                select string_agg(pg_catalog.pg_get_constraintdef(dc.oid, true), ' ')
                from pg_catalog.pg_constraint dc where dc.contypid = t.oid
            ) end                                              as constraint,
            case when t.typtype = 'c' then array(
                select a.attname from pg_catalog.pg_attribute a
                where a.attrelid = t.typrelid and a.attnum > 0 and not a.attisdropped
                order by a.attnum
            ) end                                              as attr_names,
            case when t.typtype = 'c' then array(
                select pg_catalog.format_type(a.atttypid, a.atttypmod)
                from pg_catalog.pg_attribute a
                where a.attrelid = t.typrelid and a.attnum > 0 and not a.attisdropped
                order by a.attnum
            ) end                                              as attr_types,
            pg_catalog.obj_description(t.oid, 'pg_type')       as comment
        from pg_catalog.pg_type t
            join pg_catalog.pg_namespace n on n.oid = t.typnamespace
            left join pg_catalog.pg_class c on c.oid = t.typrelid
        where n.nspname = any(%(schemas)s)
          and t.typtype = any(%(typtypes)s)
          and (t.typtype <> 'c' or c.relkind = 'c')
          and t.oid > %(after)s
        order by t.oid
        limit %(limit)s
    """)

    ARG_TYPE: ClassVar[sql.SQL] = sql.SQL("""
        select pg_catalog.format_type(t.oid, null) as type
        from unnest(%(oids)s::oid[]) with ordinality as u(oid, n)
            join pg_catalog.pg_type t on t.oid = u.oid
        order by u.n
    """)


class Scope(BaseModel):
    """Что снимать: схемы по списку вызова или все пользовательские."""

    model_config = ConfigDict(frozen=True)

    schemas: tuple[str, ...]

    @classmethod
    def parse(cls, raw: str) -> Scope:
        names: list[str] = []
        for piece in raw.split(","):
            name = piece.strip()
            if name == "":
                continue

            names.append(name)

        return cls(schemas=tuple(names))


class Pacing(BaseModel):
    """Размер порции и пауза между порциями из аргументов вызова."""

    model_config = ConfigDict(frozen=True)

    batch_size: int = Field(ge=Batching.MIN_BATCH, le=Batching.MAX_BATCH)
    pause_ms: int = Field(ge=Batching.MIN_PAUSE_MS, le=Batching.MAX_PAUSE_MS)

    @property
    def pause_seconds(self) -> float:
        return self.pause_ms / Batching.MS_PER_SECOND


class Counts(BaseModel):
    """Счётчики плана и итога по частям снимка."""

    model_config = ConfigDict(frozen=True)

    relations: int
    routines: int
    sequences: int
    types: int

    @property
    def objects(self) -> int:
        return self.relations + self.routines + self.sequences + self.types


class Reader:
    """Обход каталогов одного подключения порциями с паузой между ними."""

    def __init__(
        self,
        conn: psycopg.AsyncConnection[Any],
        scope: Scope,
        emitter: SyncEmitter,
        pacing: Pacing,
    ) -> None:
        self._conn = conn
        self._database = ""
        self._scope = scope
        self._emitter = emitter
        self._batch_size = pacing.batch_size
        self._pause = pacing.pause_seconds

    async def _rows(
        self, query: sql.SQL, params: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        async with self._conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            return await cur.fetchall()

    async def _pause_between_batches(self) -> None:
        if self._pause > 0:
            await asyncio.sleep(self._pause)

    async def database(self) -> tuple[PgDatabase, str]:
        """База подключения и версия сервера; имя базы идёт во все записи."""
        rows = await self._rows(SnapshotSql.DATABASE, {})
        row = rows[0]
        self._database = str(row["name"])
        record = PgDatabase(
            name=row["name"],
            owner=row["owner"],
            encoding=row["encoding"],
            collate=row["collate"],
            comment=row["comment"],
        )
        return record, str(row["server_version"])

    async def resolve_schemas(self) -> tuple[str, ...]:
        if self._scope.schemas:
            return self._scope.schemas

        rows = await self._rows(
            SnapshotSql.USER_SCHEMAS,
            {
                "system": list(SystemSchema.names()),
                "temp": f"{SystemSchema.TEMP_PREFIX.value}%",
                "toast_temp": f"{SystemSchema.TOAST_TEMP_PREFIX.value}%",
            },
        )
        names: list[str] = []
        for row in rows:
            names.append(str(row["nspname"]))

        return tuple(names)

    async def counts(self, schemas: Sequence[str]) -> Counts:
        rows = await self._rows(
            SnapshotSql.COUNTS,
            {
                "schemas": list(schemas),
                "relkinds": Relkind.values(),
                "typtypes": Typtype.values(),
            },
        )
        row = rows[0]
        return Counts(
            relations=int(row["relations"]),
            routines=int(row["routines"]),
            sequences=int(row["sequences"]),
            types=int(row["types"]),
        )

    async def schemas(self, names: Sequence[str]) -> None:
        rows = await self._rows(SnapshotSql.SCHEMAS, {"schemas": list(names)})
        records: list[SourceRecord] = []
        for row in rows:
            records.append(
                PgSchema(
                    database=self._database,
                    name=row["name"],
                    owner=row["owner"],
                    comment=row["comment"],
                )
            )

        self._emitter.batch(PgPart.SCHEMAS, records)

    async def relations(self, schemas: Sequence[str]) -> None:
        """Отношения порциями по oid; за каждой порцией — её колонки,
        ограничения и индексы, потом пауза."""
        after = 0
        while True:
            rows = await self._rows(
                SnapshotSql.RELATIONS,
                {
                    "schemas": list(schemas),
                    "relkinds": Relkind.values(),
                    "after": after,
                    "limit": self._batch_size,
                },
            )
            if not rows:
                return

            records = list(self._relation_records(rows))
            self._emitter.batch(PgPart.RELATIONS, records)

            oids = [int(row["oid"]) for row in rows]
            await self._columns(oids)
            await self._constraints(oids)
            await self._indexes(oids)

            after = oids[-1]
            await self._pause_between_batches()

    def _relation_records(
        self, rows: Sequence[Mapping[str, Any]]
    ) -> Iterator[PgRelation]:
        for row in rows:
            relkind = Relkind(str(row["relkind"]))
            persistence = Persistence(str(row["persistence"]))
            options: dict[str, str] = {}
            raw_options = row["options"]
            if raw_options is not None:
                for item in raw_options:
                    key, _, value = str(item).partition("=")
                    options[key] = value

            yield PgRelation(
                database=self._database,
                schema_name=row["schema_name"],
                name=row["name"],
                kind=relkind.relation_kind(is_partition=bool(row["is_partition"])),
                owner=row["owner"],
                comment=row["comment"],
                tablespace=row["tablespace"],
                persistence=persistence.label(),
                row_estimate=int(row["row_estimate"]),
                total_bytes=int(row["total_bytes"]),
                partition_key=row["partition_key"],
                partition_of=row["partition_of"],
                partition_bound=row["partition_bound"],
                definition=row["definition"],
                check_option=options.get("check_option"),
                populated=row["populated"],
                foreign_server=row["foreign_server"],
                options=options,
            )

    async def _columns(self, oids: Sequence[int]) -> None:
        rows = await self._rows(SnapshotSql.COLUMNS, {"oids": list(oids)})
        records: list[SourceRecord] = []
        for row in rows:
            identity = None
            if row["identity"]:
                identity = Identity(str(row["identity"])).label()

            generated = None
            if row["generated"]:
                generated = Generated(str(row["generated"])).label()

            records.append(
                PgColumn(
                    database=self._database,
                    schema_name=row["schema_name"],
                    relation=row["relation"],
                    name=row["name"],
                    ordinal=int(row["ordinal"]),
                    type=row["type"],
                    nullable=bool(row["nullable"]),
                    default=row["default"],
                    identity=identity,
                    generated=generated,
                    collation=row["collation"],
                    comment=row["comment"],
                )
            )

        self._emitter.batch(PgPart.COLUMNS, records)

    async def _constraints(self, oids: Sequence[int]) -> None:
        rows = await self._rows(
            SnapshotSql.CONSTRAINTS, {"oids": list(oids), "contypes": Contype.values()}
        )
        records: list[SourceRecord] = []
        for row in rows:
            contype = Contype(str(row["contype"]))
            ref_columns = None
            if row["ref_columns"] is not None:
                ref_columns = tuple(str(name) for name in row["ref_columns"])

            on_update = None
            if row["on_update"]:
                on_update = ForeignAction(str(row["on_update"])).label()

            on_delete = None
            if row["on_delete"]:
                on_delete = ForeignAction(str(row["on_delete"])).label()

            records.append(
                PgConstraint(
                    database=self._database,
                    schema_name=row["schema_name"],
                    relation=row["relation"],
                    name=row["name"],
                    kind=contype.constraint_kind(),
                    columns=tuple(str(name) for name in row["columns"]),
                    ref_schema=row["ref_schema"],
                    ref_relation=row["ref_relation"],
                    ref_columns=ref_columns,
                    on_update=on_update,
                    on_delete=on_delete,
                    deferrable=bool(row["deferrable"]),
                    initially_deferred=bool(row["initially_deferred"]),
                    definition=row["definition"],
                    comment=row["comment"],
                )
            )

        self._emitter.batch(PgPart.CONSTRAINTS, records)

    async def _indexes(self, oids: Sequence[int]) -> None:
        rows = await self._rows(SnapshotSql.INDEXES, {"oids": list(oids)})
        records: list[SourceRecord] = []
        for row in rows:
            records.append(
                PgIndex(
                    database=self._database,
                    schema_name=row["schema_name"],
                    relation=row["relation"],
                    name=row["name"],
                    method=row["method"],
                    unique=bool(row["unique"]),
                    primary=bool(row["primary"]),
                    columns=tuple(str(name) for name in row["columns"]),
                    predicate=row["predicate"],
                    definition=row["definition"],
                    total_bytes=int(row["total_bytes"]),
                    comment=row["comment"],
                )
            )

        self._emitter.batch(PgPart.INDEXES, records)

    async def routines(self, schemas: Sequence[str]) -> None:
        after = 0
        while True:
            rows = await self._rows(
                SnapshotSql.ROUTINES,
                {"schemas": list(schemas), "after": after, "limit": self._batch_size},
            )
            if not rows:
                return

            records: list[SourceRecord] = []
            args: list[SourceRecord] = []
            for row in rows:
                records.append(self._routine_record(row))
                args.extend(await self._routine_args(row))

            self._emitter.batch(PgPart.ROUTINES, records)
            self._emitter.batch(PgPart.ROUTINE_ARGS, args)

            after = int(rows[-1]["oid"])
            await self._pause_between_batches()

    def _routine_record(self, row: Mapping[str, Any]) -> PgRoutine:
        prokind = Prokind(str(row["prokind"]))
        rows_estimate = None
        if row["rows"] is not None:
            rows_estimate = float(row["rows"])

        definition = row["definition"]
        if definition is None:
            definition = ""

        return PgRoutine(
            database=self._database,
            schema_name=row["schema_name"],
            name=row["name"],
            signature=row["signature"],
            kind=prokind.routine_kind(),
            owner=row["owner"],
            language=row["language"],
            arguments=row["arguments"],
            returns=row["returns"],
            returns_set=bool(row["returns_set"]),
            volatility=Volatility(str(row["volatility"])).label(),
            strict=bool(row["strict"]),
            security_definer=bool(row["security_definer"]),
            parallel=Parallel(str(row["parallel"])).label(),
            cost=float(row["cost"]),
            rows=rows_estimate,
            body=row["body"],
            definition=definition,
            comment=row["comment"],
        )

    async def _routine_args(self, row: Mapping[str, Any]) -> list[SourceRecord]:
        """Аргументы рутины по позициям: имена, режимы и типы из pg_proc;
        значения по умолчанию видны в тексте arguments."""
        type_oids = list(row["argtypes"])
        if not type_oids:
            return []

        typed = await self._rows(SnapshotSql.ARG_TYPE, {"oids": type_oids})
        names = row["argnames"]
        modes = row["argmodes"]
        records: list[SourceRecord] = []
        for position, type_row in enumerate(typed):
            name = None
            if names is not None and position < len(names) and names[position]:
                name = str(names[position])

            mode = Argmode.IN
            if modes is not None and position < len(modes):
                mode = Argmode(str(modes[position]))

            records.append(
                PgRoutineArg(
                    database=self._database,
                    schema_name=row["schema_name"],
                    routine=row["name"],
                    signature=row["signature"],
                    position=position,
                    name=name,
                    type=str(type_row["type"]),
                    mode=mode.label(),
                )
            )

        return records

    async def sequences(self, schemas: Sequence[str]) -> None:
        after = 0
        while True:
            rows = await self._rows(
                SnapshotSql.SEQUENCES,
                {"schemas": list(schemas), "after": after, "limit": self._batch_size},
            )
            if not rows:
                return

            records: list[SourceRecord] = []
            for row in rows:
                last_value = None
                if row["last_value"] is not None:
                    last_value = int(row["last_value"])

                records.append(
                    PgSequence(
                        database=self._database,
                        schema_name=row["schema_name"],
                        name=row["name"],
                        type=row["type"],
                        start=int(row["start"]),
                        minimum=int(row["minimum"]),
                        maximum=int(row["maximum"]),
                        increment=int(row["increment"]),
                        cycle=bool(row["cycle"]),
                        cache=int(row["cache"]),
                        last_value=last_value,
                        owned_by=row["owned_by"],
                        comment=row["comment"],
                    )
                )

            self._emitter.batch(PgPart.SEQUENCES, records)
            after = int(rows[-1]["oid"])
            await self._pause_between_batches()

    async def types(self, schemas: Sequence[str]) -> None:
        after = 0
        while True:
            rows = await self._rows(
                SnapshotSql.TYPES,
                {
                    "schemas": list(schemas),
                    "typtypes": Typtype.values(),
                    "after": after,
                    "limit": self._batch_size,
                },
            )
            if not rows:
                return

            records: list[SourceRecord] = []
            for row in rows:
                records.append(self._type_record(row))

            self._emitter.batch(PgPart.TYPES, records)
            after = int(rows[-1]["oid"])
            await self._pause_between_batches()

    def _type_record(self, row: Mapping[str, Any]) -> PgType:
        typtype = Typtype(str(row["typtype"]))
        labels = None
        if row["labels"] is not None:
            labels = tuple(str(label) for label in row["labels"])

        attributes = None
        if row["attr_names"] is not None:
            attributes = tuple(
                PgTypeAttribute(name=str(name), type=str(kind))
                for name, kind in zip(row["attr_names"], row["attr_types"], strict=True)
            )

        return PgType(
            database=self._database,
            schema_name=row["schema_name"],
            name=row["name"],
            kind=typtype.type_kind(),
            owner=row["owner"],
            labels=labels,
            base_type=row["base_type"],
            constraint=row["constraint"],
            attributes=attributes,
            comment=row["comment"],
        )


@tool
async def pg_schema_snapshot(
    connection: PgConnection,
    schemas: Annotated[
        str,
        Field(
            description=(
                "Схемы через запятую: `public, etl`. Пусто — все схемы базы, "
                "кроме системных (pg_catalog, information_schema, pg_toast)."
            ),
        ),
    ],
    batch_size: Annotated[
        int,
        Field(
            ge=Batching.MIN_BATCH,
            le=Batching.MAX_BATCH,
            description="Сколько объектов читать за один заход в каталог.",
        ),
    ],
    pause_ms: Annotated[
        int,
        Field(
            ge=Batching.MIN_PAUSE_MS,
            le=Batching.MAX_PAUSE_MS,
            description="Пауза между заходами в миллисекундах, чтобы не грузить базу.",
        ),
    ],
    out: Annotated[Outbound[SyncFrame], Injected],
) -> tuple[str, ToolResult]:
    """Снимает структуру базы подключения для каталога данных: схемы,
    таблицы и представления с колонками, ограничениями и индексами,
    функции и процедуры, последовательности, типы. Кадры уходят в выходной
    порт порциями; версию источника из них собирает каталог. База — та, к
    которой подключение."""
    scope = Scope.parse(schemas)
    emitter = SyncEmitter(out, PgSnapshot)

    conn = await PayloadPostgres.connect_config(connection)
    async with conn:
        pacing = Pacing(batch_size=batch_size, pause_ms=pause_ms)
        reader = Reader(conn, scope, emitter, pacing)
        database, server_version = await reader.database()
        names = await reader.resolve_schemas()
        counts = await reader.counts(names)
        emitter.plan(
            SyncPlan(
                source_kind=PgSourceKind.POSTGRES.value,
                database=database.name,
                schemas=names,
                objects_total=counts.objects,
                server_version=server_version,
            )
        )
        emitter.batch(PgPart.DATABASES, [database])
        await reader.schemas(names)
        await reader.relations(names)
        await reader.routines(names)
        await reader.sequences(names)
        await reader.types(names)
        done = emitter.done()

    summary = (
        f"snapshot of {database.name}: {len(names)} schema(s), "
        f"{done.objects_total} object(s) in {done.batches} batch(es): "
        f"{counts.relations} relations, {counts.routines} routines, "
        f"{counts.sequences} sequences, {counts.types} types"
    )
    metadata = {
        "database": database.name,
        "schemas": ", ".join(names),
        "objects": str(done.objects_total),
        "batches": str(done.batches),
    }
    return pack_result(TextResult(text=summary, metadata=metadata))


TOOLS: Final = ToolMain.toolset(pg_schema_snapshot)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
