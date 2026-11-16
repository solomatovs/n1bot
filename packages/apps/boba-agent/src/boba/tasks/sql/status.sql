with
params as not materialized (
    select
        $1::text as owner_id,
        $2::text as task_id
)
select
    to_jsonb(t) - 'worker_id' - 'lease_token' - 'lease_expires_at' as state,
    t.worker_id,
    t.lease_token,
    t.lease_expires_at,
    extract(epoch from (t.lease_expires_at - clock_timestamp()))::double precision as remaining_sec
from
    boba.tasks t
    cross join params p
where 1=1
    and t.owner_id = p.owner_id
    and t.task_id = p.task_id;
