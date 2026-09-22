select
    attrelid,
    attnum,
    attname,
    atttypid,
    atttypmod,
    format_type(atttypid, atttypmod) as data_type,
    attnotnull,
    atthasdef,
    '' as attidentity,
    '' as attgenerated,
    xmin::text as row_xmin
from
    pg_attribute
where
    attrelid = any(%(rels)s::oid[]) and attnum > 0 and not attisdropped
