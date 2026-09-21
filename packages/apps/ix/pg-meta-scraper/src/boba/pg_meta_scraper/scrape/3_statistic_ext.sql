-- @name statistic_ext
-- @wave 3
-- @params rels
-- @collect statistics oid
-- @min 100000
select
    oid,
    stxname,
    stxnamespace,
    stxrelid,
    array(select unnest(stxkeys::int2[])) as stxkeys,
    stxkind,
    xmin::text as row_xmin
from
    pg_statistic_ext
where
    stxrelid = any(%(rels)s::oid[]);
-- @verify
select
    oid,
    xmin::text as row_xmin
from
    pg_statistic_ext
where
    stxrelid = any(%(rels)s::oid[]);
