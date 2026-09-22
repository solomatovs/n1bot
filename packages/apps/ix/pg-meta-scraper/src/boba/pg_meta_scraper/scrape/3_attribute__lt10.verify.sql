select
    attrelid,
    attnum,
    xmin::text as row_xmin
from
    pg_attribute
where
    attrelid = any(%(rels)s::oid[]) and attnum > 0 and not attisdropped;
