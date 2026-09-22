select
    reloid,
    urilocation,
    fmttype,
    xmin::text as row_xmin
from
    pg_exttable
where
    reloid = any(%(rels)s::oid[])
