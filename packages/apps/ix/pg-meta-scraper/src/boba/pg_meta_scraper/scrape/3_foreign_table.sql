-- @name foreign_table
-- @wave 3
-- @params rels
-- @min 90100
select
    ftrelid,
    ftserver,
    ftoptions,
    xmin::text as row_xmin
from
    pg_foreign_table
where
    ftrelid = any(%(rels)s::oid[]);
-- @verify
select
    ftrelid,
    xmin::text as row_xmin
from
    pg_foreign_table
where
    ftrelid = any(%(rels)s::oid[]);
