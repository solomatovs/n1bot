-- Saved command catalogue at a history position. Does not execute slash commands.
-- availableCommands is used both by session setup and by subsequent updates.
with
params as (
    select
        $1::text as session_id,
        $2::bigint as through_seq
),
history as not materialized (
    select
        r.session_id,
        r.seq,
        r.record_id,
        r.kind,
        r.category,
        r.role,
        r.key,
        r.operation,
        r.request_id,
        r.copied_from_id,
        r.created_at,
        r.version,
        r.payload
    from
        history_sql.records r
    where 1=1
        and r.session_id = (select session_id from params)
),
bounds as (
    select
        p.session_id,
        coalesce(p.through_seq, h.seq, 0) as through_seq,
        h.seq as last_seq
    from
        params p
        left join lateral (
            select
                seq
            from
                history
            order by
                seq desc
            limit
                1
        ) h on true
),
latest as (
    select distinct on (h.key)
        h.key,
        h.operation,
        h.payload
    from
        history h
        cross join bounds b
    where 1=1
        and h.kind in ('command_added', 'command_removed')
        and h.seq <= b.through_seq
    order by
        h.key, h.seq desc
),
commands as (
    select
        key,
        payload
    from
        latest
    where 1=1
        and operation = 'set'
),
inputs as (
    select
        key,
        jsonb_build_object(
            'type', 'text',
            'hint', payload -> 'input_hint'
        ) as value
    from
        commands
    where 1=1
        and payload ->> 'input_hint' is not null
),
command_values as (
    select
        c.key,
        jsonb_strip_nulls(jsonb_build_object(
            'type', 'command',
            'name', c.key,
            'description', c.payload -> 'description',
            'input', i.value
        )) as value
    from
        commands c
        left join inputs i on i.key = c.key
)
select
    to_jsonb(b) as bounds,
    jsonb_build_object(
        'sessionUpdate', 'available_commands_update',
        'availableCommands', (
            select
                coalesce(jsonb_agg(value order by key), '[]'::jsonb)
            from
                command_values
        )
    ) as update
from
    bounds b;
