/*
cfl-indexer, рёбра, шаг 2: разрешить цели и вставить рёбра. Цель по id — node с адресом
сервера и content; цель по заголовку — cfl_page этого спейса с таким title.
Неразрешённые и ссылки на себя отбрасываются; пара src -> tgt одна, вид ссылки берётся
первый по алфавиту.
*/
-- @name links_apply
-- @params base space_key
with resolved as (
    select
        l.src_node,
        coalesce(by_id.id, by_title.node_id) as tgt_node,
        l.kind
    from
        links l
        left join {schema}.node by_id
            on  l.target_id <> ''
            and by_id.address = %(base)s::jsonb || jsonb_build_object('content', l.target_id)
        left join {schema}.cfl_page by_title
            on  l.target_id = ''
            and by_title.space_key = %(space_key)s
            and by_title.title = l.target_title
),
pairs as (
    select
        src_node,
        tgt_node,
        min(kind) as kind
    from
        resolved
    where
        tgt_node is not null
        and tgt_node <> src_node
    group by
        src_node, tgt_node
),
added as (
    insert into {schema}.edge
        (node_src_id, node_tgt_id, surface, weight)
    select
        src_node, tgt_node, 'cfl_page_link', 1.0
    from
        pairs
    on conflict (node_src_id, node_tgt_id) do nothing
    returning id, node_src_id, node_tgt_id
),
kinds as (
    insert into {schema}.cfl_page_link
        (edge_id, kind)
    select
        a.id, p.kind
    from
        added a
        join pairs p on p.src_node = a.node_src_id and p.tgt_node = a.node_tgt_id
    returning 1
)
select
    (select count(*) from kinds) as linked,
    (select count(*) from links) - (select count(*) from resolved where tgt_node is not null) as unresolved;
