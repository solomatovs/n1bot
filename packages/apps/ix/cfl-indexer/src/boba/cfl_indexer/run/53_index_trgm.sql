/*
cfl-indexer, индекс node, шаг 4: аспекты классов ident и words в триграммы.
*/
-- @name index_trgm
-- @params node_id
insert into {schema}.cfl_idx_trgm
    (node_id, surface, aspect, content)
select
    a.node_id,
    a.surface,
    a.aspect,
    a.content
from
    ({sources}) a
    join {schema}.aspect x on x.aspect = a.aspect
where
    a.node_id = %(node_id)s
    and x.class in ('ident', 'words')
on conflict (node_id, surface, aspect) do nothing;
