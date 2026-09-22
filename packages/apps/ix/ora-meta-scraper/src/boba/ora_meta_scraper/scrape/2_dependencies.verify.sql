select
    d.d_obj# as d_obj_id,
    d.p_obj# as p_obj_id,
    rawtohex(standard_hash(d.d_obj# || '|' || d.p_obj# || '|' || d.property, 'MD5')) as row_version
from
    sys.dependency$ d
where
    d.d_obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (2, 4, 5, 6, 7, 8, 9, 12, 13, 42) and {objects})
    and d.p_obj# in (select o.obj# from sys.obj$ o where o.owner# in {owners} and o.type# in (2, 4, 5, 6, 7, 8, 9, 12, 13, 42) and {objects})
