-- @name gp_distribution_policy
-- @wave 3
-- @params rels
-- @key localoid
-- @only gp
select localoid, policytype, numsegments, array(select unnest(distkey::int2[])) as distkey, xmin::text as row_xmin
from gp_distribution_policy
where localoid = any($1);
-- @verify
select localoid, xmin::text as row_xmin
from gp_distribution_policy
where localoid = any($1);
