select
    localoid,
    xmin::text as row_xmin
from
    gp_distribution_policy
where
    localoid = any(%(rels)s::oid[])
