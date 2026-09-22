select
    oid,
    xmin::text as row_xmin
from
    pg_language;
