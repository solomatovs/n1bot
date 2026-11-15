with
params as (
    select
        $1::text as owner_id,
        $2::text as provider_id
)
update boba.providers r
set
    enabled = false,
    updated_at = now()
from
    params p
where 1=1
    and r.owner_id = p.owner_id
    and r.provider_id = p.provider_id
returning
    r.provider_id;
