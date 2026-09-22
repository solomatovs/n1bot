select
    localoid,
    policytype,
    numsegments,
    array(select unnest(distkey::int2[])) as distkey,
    xmin::text as row_xmin
from
    gp_distribution_policy
where
    localoid = any(%(rels)s::oid[]);
