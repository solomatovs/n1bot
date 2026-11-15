with
params as (
    select
        $1::text as owner_id,
        $2::text as provider_id,
        $3::text as base_url,
        $4::jsonb as configuration,
        $5::boolean as enabled
)
insert into boba.providers (
    owner_id,
    provider_id,
    api_type,
    enabled,
    base_url,
    configuration
)
select
    p.owner_id,
    p.provider_id,
    'openai',
    p.enabled,
    p.base_url,
    p.configuration
        #- '{owner_id}'
        #- '{provider_id}'
        #- '{settings,kind}'
        #- '{settings,base_url}'
from
    params p
on conflict (owner_id, provider_id) do update
set
    enabled = excluded.enabled,
    api_type = excluded.api_type,
    base_url = excluded.base_url,
    configuration = excluded.configuration,
    updated_at = now()
returning
    provider_id;
