/*
cfl-indexer: ровно одна строка tree на node. Строка с другим родителем снимается,
строка с тем же остаётся; not exists смотрит снимок до удаления, и при смене родителя
он пуст для нового.
*/
-- @name tree
-- @params node_id parent_id
with gone as (
    delete from {schema}.tree
    where
        node_id = %(node_id)s
        and parent_id is distinct from %(parent_id)s
    returning 1
)
insert into {schema}.tree
    (node_id, parent_id)
select
    %(node_id)s, %(parent_id)s
where
    not exists (
        select 1
        from   {schema}.tree t
        where  t.node_id = %(node_id)s
          and  t.parent_id is not distinct from %(parent_id)s
    );
