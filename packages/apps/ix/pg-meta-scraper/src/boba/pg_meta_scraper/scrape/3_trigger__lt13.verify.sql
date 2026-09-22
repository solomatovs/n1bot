select
    oid,
    xmin::text as row_xmin
from
    pg_trigger
where
    tgrelid = any(%(rels)s::oid[]) and not tgisinternal;
