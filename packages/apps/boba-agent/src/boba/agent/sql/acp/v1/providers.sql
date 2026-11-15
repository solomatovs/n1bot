with
params as (
    select
        $1::text as owner_id
),
providers as (
    select
        r.provider_id,
        r.api_type,
        r.enabled,
        r.base_url
    from
        params p
        join boba.providers r on r.owner_id = p.owner_id
)
select
    jsonb_build_object(
        'providers', coalesce(jsonb_agg(jsonb_build_object(
            'providerId', p.provider_id,
            'supported', jsonb_build_array(p.api_type),
            'required', false,
            'current', case when p.enabled then jsonb_build_object(
                'apiType', p.api_type,
                'baseUrl', p.base_url
            ) end
        ) order by p.provider_id), '[]'::jsonb)
    ) as response
from
    providers p;
