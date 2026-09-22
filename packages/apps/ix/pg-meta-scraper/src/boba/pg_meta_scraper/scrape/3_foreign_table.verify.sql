select
    ftrelid,
    xmin::text as row_xmin
from
    pg_foreign_table
where
    ftrelid = any(%(rels)s::oid[])
