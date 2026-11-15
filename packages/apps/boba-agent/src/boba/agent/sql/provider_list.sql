with
params as (
    select
        $1::text as owner_id
)
select
    r.provider_id
from
    params p
    join boba.providers r on r.owner_id = p.owner_id
order by
    r.provider_id;
