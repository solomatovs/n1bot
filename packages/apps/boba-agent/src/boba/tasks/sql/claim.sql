with
params as not materialized (
    select
        $1::text as worker_id,
        $2::uuid as lease_token,
        $3::text[] as kinds,
        $4::text[] as routes,
        $5::text[] as owners,
        $6::boolean as all_owners,
        $7::double precision as lease_sec
),
routes as not materialized (
    select
        k.kind,
        r.route_key
    from
        params p
        cross join unnest(p.kinds) k(kind)
        cross join unnest(array_prepend(null::text, p.routes)) r(route_key)
),
owned as (
    select
        candidate.task_id,
        candidate.created_at
    from
        params p
        cross join routes r
        cross join unnest(p.owners) o(owner_id)
        cross join lateral (
            select
                t.task_id,
                t.created_at
            from
                boba.tasks t
            where 1=1
                and t.status = 'pending'
                and array[t.owner_id, t.kind, t.route_key] = array[o.owner_id, r.kind, r.route_key]
            order by
                t.created_at, t.task_id
            limit
                1
            for update of t skip locked
        ) candidate
    where 1=1
        and not p.all_owners
),
shared as (
    select
        candidate.task_id,
        candidate.created_at
    from
        params p
        cross join routes r
        cross join lateral (
            select
                t.task_id,
                t.created_at
            from
                boba.tasks t
            where 1=1
                and t.status = 'pending'
                and array[t.kind, t.route_key] = array[r.kind, r.route_key]
            order by
                t.created_at, t.task_id
            limit
                1
            for update of t skip locked
        ) candidate
    where 1=1
        and p.all_owners
),
available as (
    select
        task_id, created_at
    from
        owned
    union all
    select
        task_id, created_at
    from
        shared
),
candidate as (
    select
        task_id
    from
        available
    order by
        created_at, task_id
    limit
        1
),
claimed as (
    update boba.tasks t
    set
        status                  = 'running',
        worker_id               = p.worker_id,
        lease_token             = p.lease_token,
        started_at              = clock_timestamp(),
        execution_deadline_at   = clock_timestamp() + make_interval(secs => t.run_timeout_sec),
        lease_expires_at        = clock_timestamp() + make_interval(secs => least(p.lease_sec, t.run_timeout_sec)),
        revision                = t.revision + 1
    from
        candidate c
        cross join params p
    where 1=1
        and t.task_id = c.task_id
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
    claimed t;
