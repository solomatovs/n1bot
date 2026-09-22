select
    sys_context('userenv', 'con_name') as con_name,
    sys_context('userenv', 'db_name') as db_name,
    r.version,
    p.value$ as charset,
    rawtohex(standard_hash(sys_context('userenv', 'con_name') || '|' || sys_context('userenv', 'db_name') || '|' || r.version || '|' || p.value$, 'MD5')) as row_version
from
    sys.registry$ r, sys.props$ p
where
    r.cid = 'CATALOG' and p.name = 'NLS_CHARACTERSET'
