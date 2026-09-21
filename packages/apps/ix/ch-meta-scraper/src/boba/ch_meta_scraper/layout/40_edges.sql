-- Все рёбра: src зависит от tgt, у каждого ребра роль. Позиции колонок в ключах
-- каталог не отдаёт (только флаги is_in_*), выражения ключей и индексов лежат
-- текстом в поверхностях.

-- Таблица перечисляет колонки своих ключей
insert into stage_edge
select
    t.address, c.address, r.role
from
    raw_columns rc
    join lateral (
        values
            ('partition_key', rc.is_in_partition_key),
            ('sorting_key',   rc.is_in_sorting_key),
            ('primary_key',   rc.is_in_primary_key),
            ('sampling_key',  rc.is_in_sampling_key)
    ) as r(role, flag) on r.flag = 1
    join stage_node t
        on  t.kind = 'rel'
        and t.database = rc.database
        and t.relation = rc.table_name
    join stage_node c
        on  c.kind = 'col'
        and c.database = rc.database
        and c.relation = rc.table_name
        and c.name = rc.name
on conflict do nothing;

-- dependencies_*: объекты, зависящие от таблицы (материализованные представления
-- над ней): src зависимый, tgt таблица
insert into stage_edge
select
    dep.address, t.address, 'dependency'
from
    raw_tables rt
    join lateral unnest(rt.dependencies_database, rt.dependencies_table)
        as d(db, tbl) on true
    join stage_node t
        on  t.kind in ('rel', 'dict')
        and t.database = rt.database
        and t.relation = rt.name
    join stage_node dep
        on  dep.kind in ('rel', 'dict')
        and dep.database = d.db
        and dep.relation = d.tbl
where
    dep.address <> t.address
on conflict do nothing;

-- loading_dependencies_*: без чего объект не загрузится (целевая таблица
-- представления, таблица-источник словаря): src объект, tgt зависимость
insert into stage_edge
select
    t.address, dep.address, 'loading'
from
    raw_tables rt
    join lateral unnest(rt.loading_dependencies_database, rt.loading_dependencies_table)
        as d(db, tbl) on true
    join stage_node t
        on  t.kind in ('rel', 'dict')
        and t.database = rt.database
        and t.relation = rt.name
    join stage_node dep
        on  dep.kind in ('rel', 'dict')
        and dep.database = d.db
        and dep.relation = d.tbl
where
    dep.address <> t.address
on conflict do nothing;

-- target_*: куда пишет материализованное представление (с ClickHouse 26.6)
insert into stage_edge
select
    v.address, t.address, 'target'
from
    raw_tables rt
    join stage_node v
        on  v.kind = 'rel'
        and v.database = rt.database
        and v.relation = rt.name
    join stage_node t
        on  t.kind = 'rel'
        and t.database = rt.target_database
        and t.relation = rt.target_table
where
    rt.target_table is not null
    and rt.target_table <> ''
    and v.address <> t.address
on conflict do nothing;
