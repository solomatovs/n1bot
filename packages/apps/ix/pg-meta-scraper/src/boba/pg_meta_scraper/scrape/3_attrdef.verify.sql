select
    oid,
    xmin::text as row_xmin
from
    pg_attrdef
where
    adrelid = any(%(rels)s::oid[])
