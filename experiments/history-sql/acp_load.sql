-- Run fixtures.sql first. Replace only the named load histories in an isolated database.
begin;

delete
from
    boba.records
where 1=1
    and session_id in ('sql-acp-load-1000', 'sql-acp-load-10000', 'sql-acp-load-50000');

insert into boba.records (
    session_id,
    seq,
    record_id,
    operation_id,
    operation_position,
    parent_operation_id,
    kind,
    category,
    role,
    scope,
    key,
    operation,
    copied_from_id,
    created_at,
    version,
    payload
)
with
sizes(session_id, rounds) as (
    values
        ('sql-acp-load-1000', 50),
        ('sql-acp-load-10000', 500),
        ('sql-acp-load-50000', 2500)
),
source as materialized (
    select
        r.seq,
        r.record_id,
        r.operation_id,
        r.operation_position,
        r.parent_operation_id,
        r.kind,
        r.category,
        r.role,
        r.scope,
        r.key,
        r.operation,
        r.created_at,
        r.version,
        r.payload
    from
        boba.records r
    where 1=1
        and r.session_id = 'sql-acp-demo'
),
rounds as (
    select
        s.session_id,
        n.number,
        '#' || n.number as suffix
    from
        sizes s
        cross join lateral generate_series(1, s.rounds) n(number)
),
reference_fields(field) as (
    values
        ('assistant_record_id'),
        ('call_record_id'),
        ('call_id'),
        ('related_message_id'),
        ('permission_request_id'),
        ('elicitation_id'),
        ('command_id')
)
select
    n.session_id,
    (n.number - 1) * (select max(seq) from source) + r.seq,
    r.record_id || n.suffix,
    r.operation_id || n.suffix,
    r.operation_position,
    r.parent_operation_id || n.suffix,
    r.kind,
    r.category,
    r.role,
    r.scope,
    case
        when r.category in ('tool_call', 'tool_result') then r.key || n.suffix
        else r.key
    end,
    r.operation,
    null,
    r.created_at,
    r.version,
    r.payload || coalesce(refs.value, '{}'::jsonb) || coalesce(parts.value, '{}'::jsonb)
from
    rounds n
    cross join source r
    left join lateral (
        select
            jsonb_object_agg(f.field, (r.payload ->> f.field) || n.suffix) as value
        from
            reference_fields f
        where 1=1
            and r.payload ->> f.field is not null
    ) refs on true
    left join lateral (
        select
            jsonb_build_object('content', jsonb_agg(
                p.value || jsonb_strip_nulls(jsonb_build_object(
                    'call_id', (p.value ->> 'call_id') || n.suffix
                ))
                order by p.position
            )) as value
        from
            jsonb_array_elements(case
                when jsonb_typeof(r.payload -> 'content') = 'array' then r.payload -> 'content'
                else '[]'::jsonb
            end) with ordinality p(value, position)
        having
            count(1) > 0
    ) parts on true
order by
    n.session_id,
    n.number,
    r.seq;

-- Independent client histories for mixed reader/writer runs.
delete
from
    boba.records r
using
    generate_series(1, 512) n(number)
where 1=1
    and r.session_id = 'sql-acp-stress-' || lpad(n.number::text, 4, '0');

insert into boba.records (
    session_id,
    seq,
    record_id,
    operation_id,
    operation_position,
    parent_operation_id,
    kind,
    category,
    role,
    scope,
    key,
    operation,
    copied_from_id,
    created_at,
    version,
    payload
)
with
sessions as (
    select
        'sql-acp-stress-' || lpad(n.number::text, 4, '0') as session_id,
        case
            when (n.number - 1) % 128 = 0 then 'sql-acp-load-50000'
            when (n.number - 1) % 16 = 0 then 'sql-acp-load-10000'
            else 'sql-acp-load-1000'
        end as source_session_id
    from
        generate_series(1, 512) n(number)
)
select
    s.session_id,
    r.seq,
    r.record_id,
    r.operation_id,
    r.operation_position,
    r.parent_operation_id,
    r.kind,
    r.category,
    r.role,
    r.scope,
    r.key,
    r.operation,
    r.copied_from_id,
    r.created_at,
    r.version,
    r.payload
from
    sessions s
    join boba.records r on r.session_id = s.source_session_id
order by
    s.session_id,
    r.seq;

commit;
