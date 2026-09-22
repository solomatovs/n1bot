select
    t.ts# as ts_id,
    t.name,
    rawtohex(standard_hash(t.ts# || '|' || t.name, 'MD5')) as row_version
from
    sys.ts$ t
