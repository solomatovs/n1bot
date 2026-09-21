-- @name seen_mark
-- @params node_id
insert into seen
    (node_id)
values
    (%(node_id)s)
on conflict (node_id) do nothing;
