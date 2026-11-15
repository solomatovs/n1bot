with
params as (
    select
        $1::text as owner_id,
        $2::text as provider_id
)
select
    r.enabled,
    r.configuration || jsonb_build_object(
        'owner_id', r.owner_id,
        'provider_id', r.provider_id,
        'settings', (r.configuration -> 'settings') || jsonb_build_object(
            'kind', r.api_type,
            'base_url', r.base_url
        )
    ) as configuration
from
    params p
    join boba.providers r on r.provider_id = p.provider_id
where 1=1
    and r.owner_id = p.owner_id;
