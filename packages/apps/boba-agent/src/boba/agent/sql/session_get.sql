with
params as (
    select
        $1::text as session_id,
        $2::text as owner_id
)
select
    s.session_id,
    s.owner_id,
    s.cwd
from
    params p
    join boba.sessions s on s.session_id = p.session_id
    left join boba.records d on d.session_id = s.session_id and d.kind = 'session_deleted'
where 1=1
    and s.owner_id = p.owner_id
    and d.seq is null;
