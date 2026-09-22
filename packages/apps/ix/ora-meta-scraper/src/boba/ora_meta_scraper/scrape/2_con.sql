select
    c.con# as con_id,
    c.owner# as owner_id,
    c.name,
    rawtohex(standard_hash(c.con# || '|' || c.owner# || '|' || c.name, 'MD5')) as row_version
from
    sys.con$ c
where
    c.owner# in {owners}
