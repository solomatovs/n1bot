-- База и схемы
insert into stage_node (kind, oid, surface, address)
select 'db', d.oid, 'pg_database', a
from raw_database d, raw_source s,
     lateral (select jsonb_build_object('scheme', s.scheme, 'host', s.host, 'port', s.port, 'database', s.database)) as x(a);

insert into stage_node (kind, oid, surface, address)
select 'nsp', ns.oid, 'pg_schema', a
from raw_namespace ns, raw_source s,
     lateral (select jsonb_build_object('scheme', s.scheme, 'host', s.host, 'port', s.port, 'database', s.database,
                                        'schema', ns.nspname)) as x(a);

-- Отношения: таблицы (r, p, f), view (v, m), sequence (S), индексы (i, I). relkind c это тип, он ниже.
insert into stage_node (kind, oid, surface, address)
select 'rel', c.oid, case when c.relkind in ('v', 'm') then 'pg_view'
            when c.relkind = 'S' then 'pg_sequence'
            when c.relkind in ('i', 'I') then 'pg_index'
            else 'pg_table' end::ix.surface_e, a
from raw_class c
join raw_namespace ns on ns.oid = c.relnamespace, raw_source s,
     lateral (select jsonb_build_object('scheme', s.scheme, 'host', s.host, 'port', s.port, 'database', s.database,
                                        'schema', ns.nspname, case when c.relkind in ('v', 'm') then 'view'
                    when c.relkind = 'S' then 'sequence'
                    when c.relkind in ('i', 'I') then 'index'
                    else 'table' end, c.relname)) as x(a)
where c.relkind in ('r', 'p', 'f', 'v', 'm', 'S', 'i', 'I');

-- Колонки таблиц и view
insert into stage_node (kind, oid, subid, surface, address)
select 'col', at.attrelid, at.attnum, 'pg_column', a
from raw_attribute at
join stage_node r on r.kind = 'rel' and r.oid = at.attrelid and r.surface in ('pg_table', 'pg_view'),
     lateral (select r.address || jsonb_build_object('column', at.attname)) as x(a);

-- Constraint'ы таблиц (под таблицей) и доменов (под типом)
insert into stage_node (kind, oid, surface, address)
select 'con', con.oid, 'pg_constraint', a
from raw_constraint con
join stage_node r on r.kind = 'rel' and r.oid = con.conrelid,
     lateral (select r.address || jsonb_build_object('constraint', con.conname)) as x(a)
where con.conrelid <> 0;

insert into stage_node (kind, oid, surface, address)
select 'typ', t.oid, 'pg_type', a
from raw_type t
join raw_namespace ns on ns.oid = t.typnamespace, raw_source s,
     lateral (select jsonb_build_object('scheme', s.scheme, 'host', s.host, 'port', s.port, 'database', s.database,
                                        'schema', ns.nspname, 'type', t.typname)) as x(a)
where t.typtype in ('d', 'e', 'r', 'c') and not exists (select 1 from raw_class c where c.oid = t.typrelid and c.relkind <> 'c');

insert into stage_node (kind, oid, surface, address)
select 'con', con.oid, 'pg_constraint', a
from raw_constraint con
join stage_node t on t.kind = 'typ' and t.oid = con.contypid,
     lateral (select t.address || jsonb_build_object('constraint', con.conname)) as x(a)
where con.conrelid = 0;

-- Функции: перегрузки различаются args
insert into stage_node (kind, oid, surface, address)
select 'proc', p.oid, 'pg_routine', a
from raw_proc p
join raw_namespace ns on ns.oid = p.pronamespace, raw_source s,
     lateral (select jsonb_build_object('scheme', s.scheme, 'host', s.host, 'port', s.port, 'database', s.database,
                                        'schema', ns.nspname, 'function', p.proname, 'args', p.identity_args)) as x(a);

-- Триггеры под таблицей
insert into stage_node (kind, oid, surface, address)
select 'trg', tg.oid, 'pg_trigger', a
from raw_trigger tg
join stage_node r on r.kind = 'rel' and r.oid = tg.tgrelid,
     lateral (select r.address || jsonb_build_object('trigger', tg.tgname)) as x(a);

-- Расширенная статистика в схеме
insert into stage_node (kind, oid, surface, address)
select 'stx', sx.oid, 'pg_statistics', a
from raw_statistic_ext sx
join raw_namespace ns on ns.oid = sx.stxnamespace, raw_source s,
     lateral (select jsonb_build_object('scheme', s.scheme, 'host', s.host, 'port', s.port, 'database', s.database,
                                        'schema', ns.nspname, 'statistics', sx.stxname)) as x(a);
