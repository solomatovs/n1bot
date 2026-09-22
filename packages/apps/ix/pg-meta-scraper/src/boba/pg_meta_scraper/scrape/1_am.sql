select
    oid,
    amname,
    xmin::text as row_xmin
from
    pg_am
