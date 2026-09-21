-- @name links_mark
-- @params src_node target_id target_title kind
insert into links
    (src_node, target_id, target_title, kind)
values
    (%(src_node)s, %(target_id)s, %(target_title)s, %(kind)s);
