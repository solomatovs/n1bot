select
    c.obj# as obj_id,
    c.col# as col_id,
    c.comment$ as comment_text,
    rawtohex(standard_hash(c.obj# || '|' || c.col# || '|' || c.comment$, 'MD5')) as row_version
from
    sys.com$ c
where
    c.obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (2, 4, 42) and {objects})
