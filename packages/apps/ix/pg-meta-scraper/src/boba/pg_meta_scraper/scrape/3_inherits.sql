-- @name inherits
-- @wave 3
-- @params rels
select
    inhrelid,
    inhparent,
    inhseqno,
    xmin::text as row_xmin
from
    pg_inherits
where
    inhrelid = any(%(rels)s::oid[]);
-- @verify
select
    inhrelid,
    inhparent,
    xmin::text as row_xmin
from
    pg_inherits
where
    inhrelid = any(%(rels)s::oid[]);
