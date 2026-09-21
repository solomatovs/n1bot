/*
cfl-indexer: node по адресу. Вставка идемпотентна, id возвращается и для нового, и для
существующего node: результат data-modifying CTE главному запросу не виден, поэтому
ровно одна из двух ветвей union отдаёт строку.
*/
-- @name node
-- @params surface address
with ins as (
    insert into {schema}.node
        (surface, address)
    values
        (%(surface)s::{schema}.surface_e, %(address)s::jsonb)
    on conflict (address) do nothing
    returning id
)
select
    id
from
    ins
union all
select
    n.id
from
    {schema}.node n
where
    n.address = %(address)s::jsonb
limit
    1;
