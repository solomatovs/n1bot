-- Validate newly appended records before committing the session transaction.
with
params as (
    select
        $1::text as session_id,
        $2::bigint as from_seq,
        $3::bigint as through_seq
),
history as not materialized (
    select
        r.seq,
        r.record_id,
        r.operation_id,
        r.operation_position,
        r.parent_operation_id,
        r.kind,
        r.category,
        r.key,
        r.copied_from_id,
        r.payload
    from
        boba.records r
        join params p on p.session_id = r.session_id
),
entries as materialized (
    select
        h.seq,
        h.record_id,
        h.operation_id,
        h.operation_position,
        h.parent_operation_id,
        h.kind,
        h.category,
        h.key,
        h.copied_from_id,
        h.payload
    from
        history h
        cross join params p
    where 1=1
        and h.seq between p.from_seq and p.through_seq
),
reference_fields(field, category, kind) as (
    values
        ('permission_request_id', 'operational', 'permission_request'),
        ('elicitation_id',        'operational', 'elicitation_request'),
        ('command_id',            'operational', 'slash_command'),
        ('catalog_revision',      'tool_catalog', null),
        ('assistant_record_id',   'message',     'assistant_message'),
        ('call_record_id',        'tool_call',   null),
        ('source_record_id',      'tool_result', null),
        ('related_message_id',    null,          null)
),
links as (
    select
        e.seq,
        e.record_id,
        e.payload ->> f.field as target_id,
        f.category,
        f.kind,
        f.field
    from
        entries e
        cross join reference_fields f
    where 1=1
        and e.payload ->> f.field is not null
    union all
    select
        e.seq,
        e.record_id,
        e.copied_from_id,
        e.category,
        e.kind,
        'copied_from_id'
    from
        entries e
    where 1=1
        and e.copied_from_id is not null
),
changed_operations as (
    select distinct
        operation_id
    from
        entries
),
call_references as (
    select
        e.record_id as assistant_record_id,
        e.operation_id,
        e.copied_from_id,
        p.value ->> 'call_id' as call_id
    from
        entries e
        cross join lateral jsonb_array_elements(e.payload -> 'content') p(value)
    where 1=1
        and e.kind = 'assistant_message'
        and p.value ->> 'kind' = 'tool_call_reference'
),
call_groups as (
    select
        r.assistant_record_id,
        r.call_id,
        bool_or(r.copied_from_id is not null) as copied,
        min(c.seq) as call_seq,
        count(distinct c.record_id) as definitions,
        count(distinct t.record_id) as initial_results
    from
        call_references r
        left join entries c
            on c.category = 'tool_call'
            and c.key = r.call_id
            and c.operation_id = r.operation_id
            and c.payload ->> 'assistant_record_id' = r.assistant_record_id
        left join entries t
            on t.category = 'tool_result'
            and (r.copied_from_id is not null or t.operation_id = r.operation_id)
            and t.payload ->> 'call_record_id' = c.record_id
    group by
        r.assistant_record_id,
        r.call_id
),
operation_parents as (
    select
        h.operation_id,
        h.parent_operation_id,
        min(h.seq) as seq
    from
        history h
        join changed_operations e on e.operation_id = h.operation_id
    group by
        h.operation_id, h.parent_operation_id
),
issues as (
    select
        l.seq,
        l.record_id,
        'invalid_reference' as code,
        l.field as field,
        l.target_id as target_id
    from
        links l
        left join history p on p.record_id = l.target_id
    where 1=1
        and (
            p.seq is null
            or p.seq >= l.seq
            or (l.category is not null and p.category <> l.category)
            or (l.kind is not null and p.kind <> l.kind)
        )
    union all
    select
        e.seq,
        e.record_id,
        'invalid_parent_operation',
        'parent_operation_id',
        e.parent_operation_id
    from
        entries e
        left join lateral (
            select
                min(h.seq) as seq
            from
                history h
            where 1=1
                and h.operation_id = e.parent_operation_id
        ) p on true
    where 1=1
        and e.parent_operation_id is not null
        and (p.seq is null or p.seq >= e.seq)
    union all
    select
        min(p.seq),
        null,
        'inconsistent_operation_parent',
        'operation_id',
        p.operation_id
    from
        operation_parents p
    group by
        p.operation_id
    having
        count(1) > 1
    union all
    select
        e.seq,
        e.record_id,
        'invalid_result_source',
        'source_record_id',
        p.record_id
    from
        entries e
        join history p on p.record_id = e.payload ->> 'source_record_id'
    where 1=1
        and e.category = 'tool_result'
        and p.payload ->> 'source_record_id' is not null
    union all
    select
        e.seq,
        e.record_id,
        'invalid_tool_result_reference',
        'call_record_id',
        p.record_id
    from
        entries e
        join history p on p.record_id = e.payload ->> 'call_record_id'
    where 1=1
        and e.category = 'tool_result'
        and e.key <> p.key
    union all
    select
        e.seq,
        e.record_id,
        'invalid_permission_option',
        'option_id',
        e.payload ->> 'option_id'
    from
        entries e
        join history q on q.record_id = e.payload ->> 'permission_request_id'
        left join lateral jsonb_array_elements(q.payload -> 'options') o(value)
            on o.value ->> 'option_id' = e.payload ->> 'option_id'
    where 1=1
        and e.kind = 'permission_response'
        and e.payload ->> 'action' = 'selected'
        and o.value is null
    union all
    select
        g.call_seq,
        g.assistant_record_id,
        'incomplete_tool_group',
        'call_id',
        g.call_id
    from
        call_groups g
    where 1=1
        and (g.definitions <> 1 or g.initial_results = 0 or (not g.copied and g.initial_results <> 1))
    union all
    select
        min(h.seq),
        null,
        'invalid_operation_positions',
        'operation_id',
        h.operation_id
    from
        history h
        join changed_operations e on e.operation_id = h.operation_id
    group by
        h.operation_id
    having
        min(h.operation_position) <> 0
        or max(h.operation_position) <> count(1) - 1

)
select
    coalesce(jsonb_agg(to_jsonb(i) order by i.seq, i.code), '[]'::jsonb) as errors
from
    issues i;
