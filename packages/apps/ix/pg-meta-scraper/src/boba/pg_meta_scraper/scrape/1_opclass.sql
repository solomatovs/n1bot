select
    oid,
    opcname,
    opcmethod,
    opcintype,
    xmin::text as row_xmin
from
    pg_opclass;
