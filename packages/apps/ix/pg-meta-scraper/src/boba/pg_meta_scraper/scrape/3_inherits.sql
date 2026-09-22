select
    inhrelid,
    inhparent,
    inhseqno,
    xmin::text as row_xmin
from
    pg_inherits
where
    inhrelid = any(%(rels)s::oid[])
