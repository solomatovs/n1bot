with
params as (
    select
        $1::text as owner_id
)
select
    jsonb_build_object(
        'sessions', coalesce(jsonb_agg(
            jsonb_build_object('sessionId', s.session_id, 'cwd', s.cwd)
        order by s.session_id
    ), '[]'::jsonb)) as response
from
    params p
    join boba.sessions s on s.owner_id = p.owner_id
    left join boba.records d on d.session_id = s.session_id and d.kind = 'session_deleted'
where 1=1
    and d.seq is null;
