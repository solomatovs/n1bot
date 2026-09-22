select
    oid,
    spcname,
    xmin::text as row_xmin
from
    pg_tablespace
