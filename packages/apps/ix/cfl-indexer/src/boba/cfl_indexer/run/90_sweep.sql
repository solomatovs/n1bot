/*
cfl-indexer, конец обхода спейса: node спейса, которых прогон не видел, удаляются;
каскад снимает tree, edge и surface-строки. Строки индексов здесь не трогаются: у
node, которого больше нет, объявлений тоже нет, и его строки снимут prune общих
индексаторов. Поисковые запросы джойнят {schema}.node, поэтому до ближайшего prune
осиротевшие строки в выдачу не попадают.

Область спейса — space_key поверхностей внутри одного сервера Confluence, сервер задан
частями адреса base (scheme, host, port и, если он есть, path).
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
done as (
    delete from {schema}.node n
    where
        n.id in (select node_id from scope)
        and n.id not in (select node_id from seen)
    returning n.id
)
select
    count(*) as swept
from
    done;
