select
    classid,
    objid,
    objsubid,
    refclassid,
    refobjid,
    refobjsubid,
    deptype,
    xmin::text as row_xmin
from
    pg_depend
where
    (refclassid = 1259
    and refobjid = any(%(rels)s::oid[])) or (classid = 2604
    and objid = any(%(attrdefs)s::oid[])) or (deptype = 'e'
    and objid = any(%(rels)s::oid[])) or (deptype = 'e'
    and objid = any(%(procs)s::oid[])) or (deptype = 'e'
    and objid = any(%(types)s::oid[]));
