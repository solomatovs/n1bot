-- @name links_source
-- @params src_node
insert into link_sources (src_node)
values (%(src_node)s)
on conflict do nothing;
