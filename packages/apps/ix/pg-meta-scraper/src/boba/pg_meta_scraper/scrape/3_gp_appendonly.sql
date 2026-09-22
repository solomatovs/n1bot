select
    relid,
    columnstore,
    xmin::text as row_xmin
from
    pg_appendonly
where
    relid = any(%(rels)s::oid[])
