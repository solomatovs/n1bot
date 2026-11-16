with
params as not materialized (
    select
        $1::text as task_id,
        $2::text as worker_id,
        $3::uuid as lease_token,
        $4::double precision as lease_sec
),
changed as (
    update boba.tasks t
    set
        lease_expires_at = least(t.execution_deadline_at, clock_timestamp() + make_interval(secs => p.lease_sec))
    from
        params p
    where 1=1
        and t.task_id = p.task_id
        and t.worker_id = p.worker_id
        and t.lease_token = p.lease_token
        and t.status = 'running'
        and t.lease_expires_at > clock_timestamp()
        and t.execution_deadline_at > clock_timestamp()
    returning
        t.task_id,
        t.owner_id,
        t.kind,
        t.route_key,
        t.payload,
        t.run_timeout_sec,
        t.status,
        t.revision,
        t.created_at,
        t.started_at,
        t.finished_at,
        t.execution_deadline_at,
        t.worker_id,
        t.lease_token,
        t.lease_expires_at,
        t.progress,
        t.result,
        t.error
)
select
    to_jsonb(t) - 'worker_id' - 'lease_token' - 'lease_expires_at' as state,
    t.worker_id,
    t.lease_token,
    t.lease_expires_at,
    extract(epoch from (t.lease_expires_at - clock_timestamp()))::double precision as remaining_sec
from
    changed t;
