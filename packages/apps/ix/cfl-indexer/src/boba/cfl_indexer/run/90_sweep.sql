/*
cfl-indexer, конец обхода спейса: node спейса, которых прогон не видел, удаляются;
каскад снимает tree, edge и surface-строки, а строки трёх индексов снимаются здесь же
по id ушедших node. Область спейса — space_key поверхностей внутри одного сервера
Confluence, сервер задан частями адреса base (scheme, host, port).
*/
-- @name sweep
-- @params space_key base
with owned as (
    select node_id from {schema}.cfl_space where space_key = %(space_key)s
    union all
    select node_id from {schema}.cfl_page where space_key = %(space_key)s
    union all
    select node_id from {schema}.cfl_blogpost where space_key = %(space_key)s
    union all
    select node_id from {schema}.cfl_attachment where space_key = %(space_key)s
    union all
    select node_id from {schema}.cfl_comment where space_key = %(space_key)s
),
scope as (
    select
        o.node_id
    from
        owned o
        join {schema}.node n
            on  n.id = o.node_id
            and n.address @> %(base)s::jsonb
),
gone as (
    delete from {schema}.node n
    where
        n.id in (select node_id from scope)
        and n.id not in (select node_id from seen)
    returning n.id
),
trgm as (
    delete from {schema}.cfl_idx_trgm
    where
        node_id in (select id from gone)
    returning 1
),
fts as (
    delete from {schema}.cfl_idx_fts
    where
        node_id in (select id from gone)
    returning 1
),
emb as (
    delete from {schema}.cfl_idx_emb_e5_1024
    where
        node_id in (select id from gone)
    returning 1
)
select
    (select count(*) from gone) as swept,
    (select count(*) from trgm) + (select count(*) from fts) + (select count(*) from emb) as index_rows;
