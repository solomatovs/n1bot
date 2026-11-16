with
params as not materialized (
    select
        $1::text as owner_id,
        $2::text as task_id,
        $3::text as kind,
        $4::text as route_key,
        $5::jsonb as payload,
        $6::double precision as run_timeout_sec
),
added as (
    insert into boba.tasks as t (
        task_id,
        owner_id,
        kind,
        route_key,
        payload,
        run_timeout_sec,
        status,
        revision,
        created_at
    )
    select
        task_id,
        owner_id,
        kind,
        route_key,
        payload,
        run_timeout_sec,
        'pending',
        1,
        clock_timestamp()
    from
        params
    on conflict (task_id) do nothing
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
    extract(epoch from (t.lease_expires_at - clock_timestamp()))::double precision as remaining_sec,
    pg_notify('boba_task_changed', jsonb_build_object('task_id', t.task_id, 'revision', t.revision)::text) is null as notified,
    pg_notify('boba_task_ready', jsonb_build_object('task_id', t.task_id, 'revision', t.revision)::text) is null as ready
from
    added t;
