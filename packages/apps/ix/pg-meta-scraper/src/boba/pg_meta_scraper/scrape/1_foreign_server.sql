select
    oid,
    srvname,
    srvfdw,
    srvoptions,
    xmin::text as row_xmin
from
    pg_foreign_server
