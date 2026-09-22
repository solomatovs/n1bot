select
    rngtypid,
    rngsubtype,
    format_type(rngsubtype, null) as subtype_name,
    xmin::text as row_xmin
from
    pg_range
where
    rngtypid = any(%(types)s::oid[])
