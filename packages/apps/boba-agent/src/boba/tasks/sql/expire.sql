with
params as not materialized (
    select
        $1::integer as batch_size,
        statement_timestamp() as at
),
due as (
    select
        t.task_id
    from
        boba.tasks t
        cross join params p
    where 1=1
        and t.status = 'running'
        and (t.lease_expires_at <= p.at or t.execution_deadline_at <= p.at)
    order by
        t.lease_expires_at, t.task_id
    limit
        (select batch_size from params)
    for update of t skip locked
),
expired as (
    update boba.tasks t
    set
        status = 'interrupted',
        finished_at = clock_timestamp(),
        revision = t.revision + 1,
        error = jsonb_build_object(
            'code', case when t.execution_deadline_at <= clock_timestamp() then 'execution_timeout' else 'lease_expired' end,
            'message', 'Task execution was interrupted; its external outcome is not confirmed.',
            'details', jsonb_build_object('worker_id', t.worker_id)
        )
    from
        due d
    where 1=1
        and t.task_id = d.task_id
        and t.status = 'running'
        and (t.lease_expires_at <= clock_timestamp() or t.execution_deadline_at <= clock_timestamp())
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
    pg_notify('boba_task_changed', jsonb_build_object('task_id', t.task_id, 'revision', t.revision)::text) is null as notified
from
    expired t;
