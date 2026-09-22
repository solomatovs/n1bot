select
    t.ts# as ts_id,
    rawtohex(standard_hash(t.ts# || '|' || t.name, 'MD5')) as row_version
from
    sys.ts$ t
