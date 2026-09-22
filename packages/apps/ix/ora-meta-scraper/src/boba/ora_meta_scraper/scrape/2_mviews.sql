select
    s.sowner as owner_name,
    s.vname as name,
    s.tname as container_name,
    s.query_len,
    s.query_txt as query_text,
    s.flag,
    s.auto_fast,
    rawtohex(standard_hash(s.sowner || '|' || s.vname || '|' || s.tname || '|' || s.query_len || '|' || s.flag || '|' || s.auto_fast, 'MD5')) as row_version
from
    sys.snap$ s
where
    s.sowner in (select u.name from sys.user$ u where u.type# = 1 and bitand(nvl(u.spare1, 0), 256) = 0)
