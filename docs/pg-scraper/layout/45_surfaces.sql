-- Стадии surface: свойства node по видам, из raw_* через stage_node. Одна temp-таблица на
-- surface, первая колонка address, дальше колонки surface-таблицы ix.<surface> по порядку.
-- Комментарии из raw_description по (objoid, classoid, objsubid): classoid это OID каталога
-- владельца (pg_class 1259, pg_type 1247, pg_proc 1255, pg_constraint 2606, pg_trigger 2620,
-- pg_namespace 2615, pg_statistic_ext 3381), база из raw_shdescription (pg_database 1262).

create temp table stage_pg_database (
    address jsonb not null primary key,
    name varchar,
    owner varchar,
    encoding varchar,
    collate_name varchar,
    ctype varchar,
    comment varchar
);
insert into stage_pg_database
select n.address, d.datname, d.owner_name, d.encoding_name, d.datcollate, d.datctype, sd.description
from raw_database d
join stage_node n on n.kind = 'db' and n.oid = d.oid
left join raw_shdescription sd on sd.objoid = d.oid and sd.classoid = 1262;

create temp table stage_pg_schema (
    address jsonb not null primary key,
    name varchar,
    owner varchar,
    comment varchar
);
insert into stage_pg_schema
select n.address, ns.nspname, ns.owner_name, dsc.description
from raw_namespace ns
join stage_node n on n.kind = 'nsp' and n.oid = ns.oid
left join raw_description dsc on dsc.objoid = ns.oid and dsc.classoid = 2615 and dsc.objsubid = 0;

create temp table stage_pg_table (
    address jsonb not null primary key,
    schema_name varchar,
    name varchar,
    kind varchar,
    owner varchar,
    tablespace varchar,
    persistence varchar,
    partition_bound varchar,
    row_estimate float8,
    pages int,
    has_index bool,
    has_triggers bool,
    distribution varchar,
    storage varchar,
    comment varchar
);
insert into stage_pg_table
select n.address, ns.nspname, c.relname,
       case when c.relkind = 'p' then 'partitioned' when c.relkind = 'f' then 'foreign' when c.relispartition then 'partition' else 'table' end,
       c.owner_name, ts.spcname,
       case c.relpersistence when 'u' then 'unlogged' when 't' then 'temporary' else 'permanent' end,
       c.relpartbound, c.reltuples, c.relpages, c.relhasindex, c.relhastriggers,
       case when gp.localoid is null then null when gp.policytype = 'r' then 'replicated'
            when coalesce(array_length(gp.distkey, 1), 0) = 0 then 'random' else 'hash' end,
       case c.relstorage when 'h' then 'heap' when 'a' then 'ao_row' when 'c' then 'ao_column' when 'x' then 'external' else am.amname end,
       dsc.description
from raw_class c
join stage_node n on n.kind = 'rel' and n.oid = c.oid and n.surface = 'pg_table'
join raw_namespace ns on ns.oid = c.relnamespace
left join raw_tablespace ts on ts.oid = c.reltablespace and c.reltablespace <> 0
left join raw_am am on am.oid = c.relam
left join raw_gp_distribution_policy gp on gp.localoid = c.oid
left join raw_description dsc on dsc.objoid = c.oid and dsc.classoid = 1259 and dsc.objsubid = 0;

create temp table stage_pg_column (
    address jsonb not null primary key,
    schema_name varchar,
    relation_name varchar,
    relation_kind varchar,
    name varchar,
    ordinal int,
    data_type varchar,
    not_null bool,
    default_expr varchar,
    identity varchar,
    generated varchar,
    comment varchar
);
insert into stage_pg_column
select n.address, ns.nspname, c.relname,
       case c.relkind when 'v' then 'view' when 'm' then 'matview' when 'f' then 'foreign' when 'p' then 'partitioned' else 'table' end,
       a.attname, a.attnum, a.data_type, a.attnotnull, ad.expr,
       case a.attidentity when 'a' then 'always' when 'd' then 'by default' else null end,
       case a.attgenerated when 's' then 'stored' when 'v' then 'virtual' else null end,
       dsc.description
from raw_attribute a
join stage_node n on n.kind = 'col' and n.oid = a.attrelid and n.subid = a.attnum
join raw_class c on c.oid = a.attrelid
join raw_namespace ns on ns.oid = c.relnamespace
left join raw_attrdef ad on ad.adrelid = a.attrelid and ad.adnum = a.attnum
left join raw_description dsc on dsc.objoid = a.attrelid and dsc.classoid = 1259 and dsc.objsubid = a.attnum;

create temp table stage_pg_view (
    address jsonb not null primary key,
    schema_name varchar,
    name varchar,
    kind varchar,
    owner varchar,
    comment varchar
);
insert into stage_pg_view
select n.address, ns.nspname, c.relname, case c.relkind when 'm' then 'matview' else 'view' end, c.owner_name, dsc.description
from raw_class c
join stage_node n on n.kind = 'rel' and n.oid = c.oid and n.surface = 'pg_view'
join raw_namespace ns on ns.oid = c.relnamespace
left join raw_description dsc on dsc.objoid = c.oid and dsc.classoid = 1259 and dsc.objsubid = 0;

create temp table stage_pg_index (
    address jsonb not null primary key,
    schema_name varchar,
    table_name varchar,
    name varchar,
    access_method varchar,
    is_unique bool,
    is_primary bool,
    is_exclusion bool,
    is_valid bool,
    columns varchar[],
    expression varchar,
    predicate varchar,
    comment varchar
);
insert into stage_pg_index
select n.address, ns.nspname, t.relname, ic.relname, am.amname, i.indisunique, i.indisprimary, i.indisexclusion, i.indisvalid,
       array(select a.attname from unnest(i.indkey) with ordinality as k(attnum, ord)
             join raw_attribute a on a.attrelid = i.indrelid and a.attnum = k.attnum
             where k.attnum > 0 order by k.ord),
       i.indexprs, i.indpred, dsc.description
from raw_index i
join raw_class ic on ic.oid = i.indexrelid
join stage_node n on n.kind = 'rel' and n.oid = i.indexrelid
join raw_class t on t.oid = i.indrelid
join raw_namespace ns on ns.oid = ic.relnamespace
left join raw_am am on am.oid = ic.relam
left join raw_description dsc on dsc.objoid = ic.oid and dsc.classoid = 1259 and dsc.objsubid = 0;

create temp table stage_pg_sequence (
    address jsonb not null primary key,
    schema_name varchar,
    name varchar,
    owner varchar,
    data_type varchar,
    start_value bigint,
    increment bigint,
    min_value bigint,
    max_value bigint,
    cycle bool,
    comment varchar
);
insert into stage_pg_sequence
select n.address, ns.nspname, c.relname, c.owner_name, sq.data_type, sq.seqstart, sq.seqincrement, sq.seqmin, sq.seqmax, sq.seqcycle, dsc.description
from raw_class c
join stage_node n on n.kind = 'rel' and n.oid = c.oid and n.surface = 'pg_sequence'
join raw_namespace ns on ns.oid = c.relnamespace
left join raw_sequence sq on sq.seqrelid = c.oid
left join raw_description dsc on dsc.objoid = c.oid and dsc.classoid = 1259 and dsc.objsubid = 0;

create temp table stage_pg_routine (
    address jsonb not null primary key,
    schema_name varchar,
    name varchar,
    kind varchar,
    language varchar,
    identity_args varchar,
    result_type varchar,
    volatility varchar,
    security_definer bool,
    owner varchar,
    comment varchar
);
insert into stage_pg_routine
select n.address, ns.nspname, p.proname,
       case p.prokind when 'p' then 'procedure' when 'a' then 'aggregate' when 'w' then 'window' else 'function' end,
       l.lanname, p.identity_args, p.result_type,
       case p.provolatile when 'i' then 'immutable' when 's' then 'stable' else 'volatile' end,
       p.prosecdef, p.owner_name, dsc.description
from raw_proc p
join stage_node n on n.kind = 'proc' and n.oid = p.oid
join raw_namespace ns on ns.oid = p.pronamespace
left join raw_language l on l.oid = p.prolang
left join raw_description dsc on dsc.objoid = p.oid and dsc.classoid = 1255 and dsc.objsubid = 0;

create temp table stage_pg_constraint (
    address jsonb not null primary key,
    schema_name varchar,
    table_name varchar,
    name varchar,
    kind varchar,
    definition varchar,
    is_deferrable bool,
    is_deferred bool,
    is_validated bool,
    on_update varchar,
    on_delete varchar,
    match_type varchar,
    comment varchar
);
insert into stage_pg_constraint
select n.address, ns.nspname, coalesce(c.relname, ty.typname), con.conname,
       case con.contype when 'p' then 'primary key' when 'u' then 'unique' when 'f' then 'foreign key' when 'c' then 'check'
                        when 'x' then 'exclusion' when 'n' then 'not null' when 't' then 'trigger' else con.contype end,
       con.definition, con.condeferrable, con.condeferred, con.convalidated,
       case when con.contype = 'f' then case con.confupdtype when 'a' then 'no action' when 'r' then 'restrict' when 'c' then 'cascade' when 'n' then 'set null' when 'd' then 'set default' end end,
       case when con.contype = 'f' then case con.confdeltype when 'a' then 'no action' when 'r' then 'restrict' when 'c' then 'cascade' when 'n' then 'set null' when 'd' then 'set default' end end,
       case when con.contype = 'f' then case con.confmatchtype when 'f' then 'full' when 'p' then 'partial' else 'simple' end end,
       dsc.description
from raw_constraint con
join stage_node n on n.kind = 'con' and n.oid = con.oid
join raw_namespace ns on ns.oid = con.connamespace
left join raw_class c on c.oid = con.conrelid and con.conrelid <> 0
left join raw_type ty on ty.oid = con.contypid and con.contypid <> 0
left join raw_description dsc on dsc.objoid = con.oid and dsc.classoid = 2606 and dsc.objsubid = 0;

create temp table stage_pg_trigger (
    address jsonb not null primary key,
    schema_name varchar,
    table_name varchar,
    name varchar,
    timing varchar,
    events varchar[],
    row_level bool,
    enabled bool,
    comment varchar
);
insert into stage_pg_trigger
select n.address, ns.nspname, c.relname, t.tgname,
       case when (t.tgtype & 2) <> 0 then 'before' when (t.tgtype & 64) <> 0 then 'instead of' else 'after' end,
       array_remove(array[case when (t.tgtype & 4) <> 0 then 'insert' end, case when (t.tgtype & 8) <> 0 then 'delete' end,
                          case when (t.tgtype & 16) <> 0 then 'update' end, case when (t.tgtype & 32) <> 0 then 'truncate' end], null),
       (t.tgtype & 1) <> 0, t.tgenabled <> 'D', dsc.description
from raw_trigger t
join stage_node n on n.kind = 'trg' and n.oid = t.oid
join raw_class c on c.oid = t.tgrelid
join raw_namespace ns on ns.oid = c.relnamespace
left join raw_description dsc on dsc.objoid = t.oid and dsc.classoid = 2620 and dsc.objsubid = 0;

create temp table stage_pg_type (
    address jsonb not null primary key,
    schema_name varchar,
    name varchar,
    kind varchar,
    base_type varchar,
    enum_labels varchar[],
    comment varchar
);
insert into stage_pg_type
select n.address, ns.nspname, ty.typname,
       case ty.typtype when 'd' then 'domain' when 'e' then 'enum' when 'c' then 'composite' when 'r' then 'range' else ty.typtype end,
       coalesce(ty.base_type, rg.subtype_name),
       case when ty.typtype = 'e' then array(select e.enumlabel from raw_enum e where e.enumtypid = ty.oid order by e.enumsortorder) end,
       dsc.description
from raw_type ty
join stage_node n on n.kind = 'typ' and n.oid = ty.oid
join raw_namespace ns on ns.oid = ty.typnamespace
left join raw_range rg on rg.rngtypid = ty.oid
left join raw_description dsc on dsc.objoid = ty.oid and dsc.classoid = 1247 and dsc.objsubid = 0;

create temp table stage_pg_statistics (
    address jsonb not null primary key,
    schema_name varchar,
    name varchar,
    table_name varchar,
    kinds varchar[],
    comment varchar
);
insert into stage_pg_statistics
select n.address, ns.nspname, sx.stxname, c.relname, sx.stxkind, dsc.description
from raw_statistic_ext sx
join stage_node n on n.kind = 'stx' and n.oid = sx.oid
join raw_namespace ns on ns.oid = sx.stxnamespace
join raw_class c on c.oid = sx.stxrelid
left join raw_description dsc on dsc.objoid = sx.oid and dsc.classoid = 3381 and dsc.objsubid = 0;
