select
    oid,
    xmin::text as row_xmin
from
    pg_database
where
    datname = current_database();
