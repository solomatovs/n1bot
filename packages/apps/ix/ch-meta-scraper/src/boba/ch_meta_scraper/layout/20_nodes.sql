-- Сервер: корень tree источника
insert into stage_node
    (kind, surface, address)
select
    'srv', 'ch_meta_server',
    jsonb_build_object('scheme', s.scheme, 'host', s.host, 'port', s.port)
from
    raw_source s;

-- Базы
insert into stage_node
    (kind, database, surface, address)
select
    'db', d.name, 'ch_meta_database',
    srv.address || jsonb_build_object('database', d.name)
from
    raw_databases d,
    stage_node srv
where
    srv.kind = 'srv';

-- Таблицы и представления. Словарь в system.tables это та же строка, что в
-- system.dictionaries, node он получает ниже из raw_dictionaries.
insert into stage_node
    (kind, database, relation, surface, address)
select
    'rel', t.database, t.name,
    case
        when t.engine in ('View', 'MaterializedView', 'LiveView', 'WindowView')
            then 'ch_meta_view'
        else 'ch_meta_table'
    end::{schema}.surface_e,
    db.address || jsonb_build_object(
        case
            when t.engine in ('View', 'MaterializedView', 'LiveView', 'WindowView')
                then 'view'
            else 'table'
        end,
        t.name
    )
from
    raw_tables t
    join stage_node db on db.kind = 'db' and db.database = t.database
where
    t.engine <> 'Dictionary';

-- Словари
insert into stage_node
    (kind, database, relation, surface, address)
select
    'dict', x.database, x.name, 'ch_meta_dictionary',
    db.address || jsonb_build_object('dictionary', x.name)
from
    raw_dictionaries x
    join stage_node db on db.kind = 'db' and db.database = x.database;

-- Колонки таблиц, представлений и словарей
insert into stage_node
    (kind, database, relation, name, surface, address)
select
    'col', c.database, c.table_name, c.name, 'ch_meta_column',
    p.address || jsonb_build_object('column', c.name)
from
    raw_columns c
    join stage_node p
        on  p.kind in ('rel', 'dict')
        and p.database = c.database
        and p.relation = c.table_name;

-- Индексы пропуска данных под таблицей
insert into stage_node
    (kind, database, relation, name, surface, address)
select
    'idx', i.database, i.table_name, i.name, 'ch_meta_index',
    p.address || jsonb_build_object('index', i.name)
from
    raw_indices i
    join stage_node p
        on  p.kind = 'rel'
        and p.surface = 'ch_meta_table'
        and p.database = i.database
        and p.relation = i.table_name;

-- Проекции под таблицей
insert into stage_node
    (kind, database, relation, name, surface, address)
select
    'proj', pr.database, pr.table_name, pr.name, 'ch_meta_projection',
    p.address || jsonb_build_object('projection', pr.name)
from
    raw_projections pr
    join stage_node p
        on  p.kind = 'rel'
        and p.surface = 'ch_meta_table'
        and p.database = pr.database
        and p.relation = pr.table_name;

-- SQL-функции пользователя: объекты сервера, базы у них нет
insert into stage_node
    (kind, name, surface, address)
select
    'fn', f.name, 'ch_meta_function',
    srv.address || jsonb_build_object('function', f.name)
from
    raw_functions f,
    stage_node srv
where
    srv.kind = 'srv';
