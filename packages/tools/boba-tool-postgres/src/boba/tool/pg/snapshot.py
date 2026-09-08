"""Снятие структуры базы Postgres в домен каталога потоком COPY.

Инструмент pg_schema_snapshot открывает подключение пользователя и
подключение к базе каталога; для каждой части снимка запрос к системным
каталогам источника отдаёт строки уже в раскладке таблицы домена, и они
перетекают COPY (select) TO STDOUT → COPY staging FROM STDIN без разбора в
модели. Staging подключения заводится заново, по концу обхода становится
новой версией подключения одной транзакцией (SnapshotWriter). База одна —
та, к которой подключение. Версия и версия сервера — в metadata итога
(SnapshotOutcome).

Ошибки:
PostgresError — до базы не достучаться (сеть, отказ libpq, kerberos).
psycopg.Error — сервер отклонил каталожный запрос (права на каталог).
CatalogDomainError — домен каталога недоступен или отказал строкам.
SnapshotConnectionError — профиль подключения не подписан строкой
    соединений: под каким id класть снимок, неизвестно.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field

from boba.catalog import NameList
from boba.db.postgres import PayloadPostgres
from boba.db.postgres.catalog import (
    CatalogStoreConfig,
    PartTable,
    SnapshotOutcome,
    SnapshotResultKey,
    SnapshotWriter,
)
from boba.db.postgres.profile import PostgresConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, UserConnection, tool
from boba.toolkit.result import MarkdownResult

PgConnection = Annotated[PostgresConfig, UserConnection]


class SnapshotConnectionError(Exception):
    """Профиль подключения без строки соединений: id для снимка нет."""


class SystemSchema(StrEnum):
    """Схемы, которые без явного списка не снимаются."""

    PG_CATALOG = "pg_catalog"
    INFORMATION_SCHEMA = "information_schema"
    PG_TOAST = "pg_toast"

    TEMP_PREFIX = "pg_temp_"
    TOAST_TEMP_PREFIX = "pg_toast_temp_"

    @classmethod
    def names(cls) -> list[str]:
        return [cls.PG_CATALOG.value, cls.INFORMATION_SCHEMA.value, cls.PG_TOAST.value]


@dataclass(frozen=True)
class PartQuery:
    """Часть снимка: таблица домена с колонками и запрос к каталогам
    источника, отдающий строки ровно в этих колонках."""

    table: PartTable
    query: sql.SQL

    @property
    def part(self) -> str:
        return self.table.part


class SnapshotSql:
    """Запросы снятия: по одному на часть, строки в раскладке таблиц домена
    (pg_databases, pg_schemas, …). Параметр schemas — список схем."""

    DATABASE: ClassVar[sql.SQL] = sql.SQL("""
        select
            d.datname                                          as name,
            pg_catalog.pg_get_userbyid(d.datdba)               as owner,
            pg_catalog.pg_encoding_to_char(d.encoding)         as encoding,
            d.datcollate                                       as collate,
            pg_catalog.shobj_description(d.oid, 'pg_database') as comment
        from pg_catalog.pg_database d
        where d.datname = current_database()
    """)

    SERVER: ClassVar[sql.SQL] = sql.SQL("""
        select
            current_database()                as database,
            current_setting('server_version') as server_version
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
                  and c.relkind in ('r', 'p', 'v', 'm', 'f'))     as relations,
            (select count(*) from pg_catalog.pg_proc p
                join pg_catalog.pg_namespace n on n.oid = p.pronamespace
                where n.nspname = any(%(schemas)s))               as routines,
            (select count(*) from pg_catalog.pg_class c
                join pg_catalog.pg_namespace n on n.oid = c.relnamespace
                where n.nspname = any(%(schemas)s)
                  and c.relkind = 'S')                            as sequences,
            (select count(*) from pg_catalog.pg_type t
                join pg_catalog.pg_namespace n on n.oid = t.typnamespace
                left join pg_catalog.pg_class c on c.oid = t.typrelid
                where n.nspname = any(%(schemas)s)
                  and t.typtype in ('e', 'd', 'c', 'r')
                  and (t.typtype <> 'c' or c.relkind = 'c'))      as types
    """)

    SCHEMAS: ClassVar[sql.SQL] = sql.SQL("""
        select
            current_database()                                 as database,
            n.nspname                                          as name,
            pg_catalog.pg_get_userbyid(n.nspowner)             as owner,
            pg_catalog.obj_description(n.oid, 'pg_namespace')  as comment
        from pg_catalog.pg_namespace n
        where n.nspname = any(%(schemas)s)
    """)

    RELATIONS: ClassVar[sql.SQL] = sql.SQL("""
        select
            current_database()                                 as database,
            n.nspname                                          as schema,
            c.relname                                          as name,
            case c.relkind
                when 'r' then
                    case when c.relispartition then 'partition' else 'table' end
                when 'p' then 'partitioned'
                when 'v' then 'view'
                when 'm' then 'materialized'
                when 'f' then 'foreign'
            end                                                as kind,
            pg_catalog.pg_get_userbyid(c.relowner)             as owner,
            pg_catalog.obj_description(c.oid, 'pg_class')      as comment,
            ts.spcname                                         as tablespace,
            case c.relpersistence
                when 'p' then 'permanent'
                when 'u' then 'unlogged'
                when 't' then 'temporary'
            end                                                as persistence,
            greatest(c.reltuples, 0)::bigint                   as row_estimate,
            pg_catalog.pg_total_relation_size(c.oid)           as total_bytes,
            case when c.relkind = 'p'
                 then pg_catalog.pg_get_partkeydef(c.oid) end  as partition_key,
            case when parent.oid is not null
                 then pn.nspname || '.' || parent.relname end  as partition_of,
            pg_catalog.pg_get_expr(c.relpartbound, c.oid)      as partition_bound,
            case when c.relkind in ('v', 'm')
                 then pg_catalog.pg_get_viewdef(c.oid, true) end as definition,
            opts.options ->> 'check_option'                    as check_option,
            case when c.relkind = 'm' then c.relispopulated end as populated,
            fs.srvname                                         as foreign_server,
            opts.options                                       as options
        from pg_catalog.pg_class c
            join pg_catalog.pg_namespace n on n.oid = c.relnamespace
            left join pg_catalog.pg_tablespace ts on ts.oid = c.reltablespace
            left join pg_catalog.pg_inherits inh on inh.inhrelid = c.oid
            left join pg_catalog.pg_class parent
                on parent.oid = inh.inhparent and c.relispartition
            left join pg_catalog.pg_namespace pn on pn.oid = parent.relnamespace
            left join pg_catalog.pg_foreign_table ft on ft.ftrelid = c.oid
            left join pg_catalog.pg_foreign_server fs on fs.oid = ft.ftserver
            cross join lateral (
                select coalesce(
                    (select jsonb_object_agg(
                        split_part(o, '=', 1), substr(o, strpos(o, '=') + 1))
                     from unnest(c.reloptions) as o),
                    '{}'::jsonb) as options
            ) opts
        where n.nspname = any(%(schemas)s)
          and c.relkind in ('r', 'p', 'v', 'm', 'f')
    """)

    COLUMNS: ClassVar[sql.SQL] = sql.SQL("""
        select
            current_database()                                 as database,
            n.nspname                                          as schema,
            c.relname                                          as relation,
            a.attname                                          as name,
            a.attnum                                           as ordinal,
            pg_catalog.format_type(a.atttypid, a.atttypmod)    as type,
            not a.attnotnull                                   as nullable,
            pg_catalog.pg_get_expr(d.adbin, d.adrelid)         as "default",
            case a.attidentity
                when 'a' then 'always'
                when 'd' then 'by default'
            end                                                as identity,
            case a.attgenerated when 's' then 'stored' end     as generated,
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
        where n.nspname = any(%(schemas)s)
          and c.relkind in ('r', 'p', 'v', 'm', 'f')
          and a.attnum > 0
          and not a.attisdropped
    """)

    CONSTRAINTS: ClassVar[sql.SQL] = sql.SQL("""
        select
            current_database()                                 as database,
            n.nspname                                          as schema,
            c.relname                                          as relation,
            con.conname                                        as name,
            case con.contype
                when 'p' then 'primary'
                when 'u' then 'unique'
                when 'f' then 'foreign'
                when 'c' then 'check'
                when 'x' then 'exclusion'
            end                                                as kind,
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
            case when con.contype = 'f' then
                case con.confupdtype
                    when 'a' then 'no action'
                    when 'r' then 'restrict'
                    when 'c' then 'cascade'
                    when 'n' then 'set null'
                    when 'd' then 'set default'
                end
            end                                                as on_update,
            case when con.contype = 'f' then
                case con.confdeltype
                    when 'a' then 'no action'
                    when 'r' then 'restrict'
                    when 'c' then 'cascade'
                    when 'n' then 'set null'
                    when 'd' then 'set default'
                end
            end                                                as on_delete,
            con.condeferrable                                  as deferrable,
            con.condeferred                                    as initially_deferred,
            pg_catalog.pg_get_constraintdef(con.oid, true)     as definition,
            pg_catalog.obj_description(con.oid, 'pg_constraint') as comment
        from pg_catalog.pg_constraint con
            join pg_catalog.pg_class c on c.oid = con.conrelid
            join pg_catalog.pg_namespace n on n.oid = c.relnamespace
            left join pg_catalog.pg_class rc on rc.oid = con.confrelid
            left join pg_catalog.pg_namespace rn on rn.oid = rc.relnamespace
        where n.nspname = any(%(schemas)s)
          and c.relkind in ('r', 'p', 'v', 'm', 'f')
          and con.contype in ('p', 'u', 'f', 'c', 'x')
    """)

    INDEXES: ClassVar[sql.SQL] = sql.SQL("""
        select
            current_database()                                 as database,
            n.nspname                                          as schema,
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
        where n.nspname = any(%(schemas)s)
          and c.relkind in ('r', 'p', 'v', 'm', 'f')
    """)

    ROUTINES: ClassVar[sql.SQL] = sql.SQL("""
        select
            current_database()                                 as database,
            n.nspname                                          as schema,
            p.proname                                          as name,
            pg_catalog.oidvectortypes(p.proargtypes)           as signature,
            case p.prokind
                when 'f' then 'function'
                when 'p' then 'procedure'
                when 'a' then 'aggregate'
                when 'w' then 'window'
            end                                                as kind,
            pg_catalog.pg_get_userbyid(p.proowner)             as owner,
            l.lanname                                          as language,
            pg_catalog.pg_get_function_arguments(p.oid)        as arguments,
            case when p.prokind <> 'p'
                 then pg_catalog.pg_get_function_result(p.oid) end as returns,
            p.proretset                                        as returns_set,
            case p.provolatile
                when 'i' then 'immutable'
                when 's' then 'stable'
                when 'v' then 'volatile'
            end                                                as volatility,
            p.proisstrict                                      as strict,
            p.prosecdef                                        as security_definer,
            case p.proparallel
                when 's' then 'safe'
                when 'r' then 'restricted'
                when 'u' then 'unsafe'
            end                                                as parallel,
            p.procost                                          as cost,
            case when p.proretset then p.prorows end           as rows,
            coalesce(p.prosrc, '')                             as body,
            case when p.prokind <> 'a'
                 then coalesce(pg_catalog.pg_get_functiondef(p.oid), '')
                 else '' end                                   as definition,
            pg_catalog.obj_description(p.oid, 'pg_proc')       as comment
        from pg_catalog.pg_proc p
            join pg_catalog.pg_namespace n on n.oid = p.pronamespace
            join pg_catalog.pg_language l on l.oid = p.prolang
        where n.nspname = any(%(schemas)s)
    """)

    ROUTINE_ARGS: ClassVar[sql.SQL] = sql.SQL("""
        select
            current_database()                                 as database,
            n.nspname                                          as schema,
            p.proname                                          as routine,
            pg_catalog.oidvectortypes(p.proargtypes)           as signature,
            u.n - 1                                            as position,
            nullif(p.proargnames[u.n], '')                     as name,
            pg_catalog.format_type(u.t, null)                  as type,
            case coalesce(p.proargmodes[u.n], 'i')
                when 'i' then 'in'
                when 'o' then 'out'
                when 'b' then 'inout'
                when 'v' then 'variadic'
                when 't' then 'table'
            end                                                as mode,
            null::text                                         as "default"
        from pg_catalog.pg_proc p
            join pg_catalog.pg_namespace n on n.oid = p.pronamespace
            cross join lateral unnest(coalesce(p.proallargtypes, p.proargtypes::oid[]))
                with ordinality as u(t, n)
        where n.nspname = any(%(schemas)s)
    """)

    SEQUENCES: ClassVar[sql.SQL] = sql.SQL("""
        select
            current_database()                                 as database,
            n.nspname                                          as schema,
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
    """)

    TYPES: ClassVar[sql.SQL] = sql.SQL("""
        select
            current_database()                                 as database,
            n.nspname                                          as schema,
            t.typname                                          as name,
            case t.typtype
                when 'e' then 'enum'
                when 'd' then 'domain'
                when 'c' then 'composite'
                when 'r' then 'range'
            end                                                as kind,
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
            case when t.typtype = 'c' then coalesce((
                select jsonb_agg(
                    jsonb_build_object(
                        'name', a.attname,
                        'type', pg_catalog.format_type(a.atttypid, a.atttypmod))
                    order by a.attnum)
                from pg_catalog.pg_attribute a
                where a.attrelid = t.typrelid and a.attnum > 0 and not a.attisdropped
            ), '[]'::jsonb) end                                as attributes,
            pg_catalog.obj_description(t.oid, 'pg_type')       as comment
        from pg_catalog.pg_type t
            join pg_catalog.pg_namespace n on n.oid = t.typnamespace
            left join pg_catalog.pg_class c on c.oid = t.typrelid
        where n.nspname = any(%(schemas)s)
          and t.typtype in ('e', 'd', 'c', 'r')
          and (t.typtype <> 'c' or c.relkind = 'c')
    """)

    PARTS: ClassVar[tuple[PartQuery, ...]] = (
        PartQuery(
            table=PartTable(
                part="databases",
                table="pg_databases",
                columns=("name", "owner", "encoding", "collate", "comment"),
            ),
            query=DATABASE,
        ),
        PartQuery(
            table=PartTable(
                part="schemas",
                table="pg_schemas",
                columns=("database", "name", "owner", "comment"),
            ),
            query=SCHEMAS,
        ),
        PartQuery(
            table=PartTable(
                part="relations",
                table="pg_relations",
                columns=(
                    "database",
                    "schema",
                    "name",
                    "kind",
                    "owner",
                    "comment",
                    "tablespace",
                    "persistence",
                    "row_estimate",
                    "total_bytes",
                    "partition_key",
                    "partition_of",
                    "partition_bound",
                    "definition",
                    "check_option",
                    "populated",
                    "foreign_server",
                    "options",
                ),
            ),
            query=RELATIONS,
        ),
        PartQuery(
            table=PartTable(
                part="columns",
                table="pg_columns",
                columns=(
                    "database",
                    "schema",
                    "relation",
                    "name",
                    "ordinal",
                    "type",
                    "nullable",
                    "default",
                    "identity",
                    "generated",
                    "collation",
                    "comment",
                ),
            ),
            query=COLUMNS,
        ),
        PartQuery(
            table=PartTable(
                part="constraints",
                table="pg_constraints",
                columns=(
                    "database",
                    "schema",
                    "relation",
                    "name",
                    "kind",
                    "columns",
                    "ref_schema",
                    "ref_relation",
                    "ref_columns",
                    "on_update",
                    "on_delete",
                    "deferrable",
                    "initially_deferred",
                    "definition",
                    "comment",
                ),
            ),
            query=CONSTRAINTS,
        ),
        PartQuery(
            table=PartTable(
                part="indexes",
                table="pg_indexes",
                columns=(
                    "database",
                    "schema",
                    "relation",
                    "name",
                    "method",
                    "unique",
                    "primary",
                    "columns",
                    "predicate",
                    "definition",
                    "total_bytes",
                    "comment",
                ),
            ),
            query=INDEXES,
        ),
        PartQuery(
            table=PartTable(
                part="routines",
                table="pg_routines",
                columns=(
                    "database",
                    "schema",
                    "name",
                    "signature",
                    "kind",
                    "owner",
                    "language",
                    "arguments",
                    "returns",
                    "returns_set",
                    "volatility",
                    "strict",
                    "security_definer",
                    "parallel",
                    "cost",
                    "rows",
                    "body",
                    "definition",
                    "comment",
                ),
            ),
            query=ROUTINES,
        ),
        PartQuery(
            table=PartTable(
                part="routine_args",
                table="pg_routine_args",
                columns=(
                    "database",
                    "schema",
                    "routine",
                    "signature",
                    "position",
                    "name",
                    "type",
                    "mode",
                    "default",
                ),
            ),
            query=ROUTINE_ARGS,
        ),
        PartQuery(
            table=PartTable(
                part="sequences",
                table="pg_sequences",
                columns=(
                    "database",
                    "schema",
                    "name",
                    "type",
                    "start",
                    "minimum",
                    "maximum",
                    "increment",
                    "cycle",
                    "cache",
                    "last_value",
                    "owned_by",
                    "comment",
                ),
            ),
            query=SEQUENCES,
        ),
        PartQuery(
            table=PartTable(
                part="types",
                table="pg_types",
                columns=(
                    "database",
                    "schema",
                    "name",
                    "kind",
                    "owner",
                    "labels",
                    "base_type",
                    "constraint",
                    "attributes",
                    "comment",
                ),
            ),
            query=TYPES,
        ),
    )

    @classmethod
    def tables(cls) -> list[PartTable]:
        tables: list[PartTable] = []
        for part in cls.PARTS:
            tables.append(part.table)

        return tables


class Scope(BaseModel):
    """Что снимать: схемы по списку вызова или все пользовательские."""

    model_config = ConfigDict(frozen=True)

    schemas: tuple[str, ...]

    @classmethod
    def parse(cls, raw: str) -> Scope:
        return cls(schemas=NameList.parse(raw))


class Source(BaseModel):
    """База источника: имя, версия сервера, схемы охвата и число объектов."""

    model_config = ConfigDict(frozen=True)

    database: str
    server_version: str
    schemas: tuple[str, ...]
    objects: int


class SourceCatalog:
    """Запросы к источнику, ответ которых нужен самому инструменту: схемы
    охвата и счётчики для итога."""

    def __init__(self, conn: psycopg.AsyncConnection[Any], scope: Scope) -> None:
        self._conn = conn
        self._scope = scope

    async def describe(self) -> Source:
        server = await self._row(SnapshotSql.SERVER, {})
        schemas = await self._schemas()
        counts = await self._row(SnapshotSql.COUNTS, {"schemas": list(schemas)})
        objects = 0
        for value in counts:
            objects += int(value)

        return Source(
            database=str(server[0]),
            server_version=str(server[1]),
            schemas=schemas,
            objects=objects,
        )

    async def _schemas(self) -> tuple[str, ...]:
        if self._scope.schemas:
            return self._scope.schemas

        async with self._conn.cursor() as cur:
            await cur.execute(
                SnapshotSql.USER_SCHEMAS,
                {
                    "system": SystemSchema.names(),
                    "temp": f"{SystemSchema.TEMP_PREFIX.value}%",
                    "toast_temp": f"{SystemSchema.TOAST_TEMP_PREFIX.value}%",
                },
            )
            rows = await cur.fetchall()

        names: list[str] = []
        for row in rows:
            names.append(str(row[0]))

        return tuple(names)

    async def _row(self, query: sql.SQL, params: dict[str, Any]) -> Sequence[Any]:
        async with self._conn.cursor() as cur:
            await cur.execute(query, params)
            row = await cur.fetchone()

        if row is None:
            text = " ".join(query.as_string(self._conn).split())
            msg = f"the source catalog query returned no row: {text}"
            raise psycopg.DataError(msg)

        return row


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
    catalog: Annotated[CatalogStoreConfig, Injected],
) -> MarkdownResult:
    """Снимает структуру базы подключения в каталог данных: схемы, таблицы
    и представления с колонками, ограничениями и индексами, функции и
    процедуры, последовательности, типы. Строки перетекают из каталогов
    базы в домен каталога потоком и ложатся новой версией подключения.
    База — та, к которой подключение."""
    if not connection.source.stored:
        msg = (
            "pg_schema_snapshot: the connection profile carries no connection "
            "row (source id is empty), the snapshot has nowhere to go"
        )
        raise SnapshotConnectionError(msg)

    scope = Scope.parse(schemas)
    store = await PayloadPostgres.connect_config(catalog.connection)
    async with store:
        writer = SnapshotWriter(
            store, catalog.db_schema, connection.source.id, SnapshotSql.tables()
        )
        await writer.open()

        conn = await PayloadPostgres.connect_config(connection)
        async with conn:
            source = await SourceCatalog(conn, scope).describe()
            params = {"schemas": list(source.schemas)}
            for part in SnapshotSql.PARTS:
                await writer.copy_from(conn, part.part, part.query, params)

        version = await writer.commit()

    outcome = SnapshotOutcome(version=version, server_version=source.server_version)
    summary = (
        f"snapshot of {source.database} written as version {version} of "
        f"connection {connection.source.name!r}: {len(source.schemas)} schema(s), "
        f"{source.objects} object(s)"
    )
    metadata = {
        SnapshotResultKey.DATABASE.value: source.database,
        SnapshotResultKey.SCHEMAS.value: NameList.render(source.schemas),
        SnapshotResultKey.OBJECTS.value: str(source.objects),
    }
    metadata.update(outcome.metadata())
    return MarkdownResult(text=summary, metadata=metadata)


TOOLS: Final = ToolMain.toolset(pg_schema_snapshot)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
