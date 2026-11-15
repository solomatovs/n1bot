-- boba.records -> openai-compatible chat request + metadata for openai_validator.py.
-- only edit params: operation_id selects overrides; through_seq is an inclusive cut.
with
params as (
    select
        'openai-tasks'::text as session_id,
        null::text as operation_id,
        null::bigint as through_seq
),
roles(role, empty_content) as (
    values
        ('system',    '""'::jsonb),
        ('user',      '""'::jsonb),
        ('assistant', 'null'::jsonb),
        ('tool',      '""'::jsonb)
),
categories(category, reduction, section, windowed, strict_remove) as (
    values
        ('message',      'sum',  1,    true,  false),
        ('assembly',     'last', null, false, false),
        ('compact',      'sum',  1,    true,  false),
        ('prompt',       'last', 0,    false, true),
        ('context',      'last', 1,    true,  false),
        ('setting',      'last', null, false, false),
        ('tool_call',    'sum',  null, false, false),
        ('tool_result',  'last', null, false, false),
        ('tool_catalog', 'last', null, false, true),
        ('tool_schema',  'last', null, false, false)
),
cut as materialized (
    select
        p.session_id,
        p.operation_id,
        p.through_seq,
        coalesce(t.seq, 0) as last_seq,
        coalesce(p.through_seq, t.seq, 0) as seq
    from
        params p
        left join lateral (
            select
                r.seq
            from
                boba.records r
            where 1=1
                and r.session_id = p.session_id
            order by
                r.seq desc
            limit
                1
        ) t on true
),
history as not materialized (
    select
        r.seq,
        r.record_id,
        r.operation_id,
        r.category,
        r.role,
        r.key,
        r.operation,
        r.scope,
        r.payload
    from
        boba.records r
        join cut c on r.session_id = c.session_id
    where 1=1
        and r.seq <= c.seq
),
bounds as materialized (
    select
        c.session_id,
        c.operation_id,
        c.seq,
        c.last_seq,
        (
            select
                min(h.seq)
            from
                history h
            where 1=1
                and h.operation_id = c.operation_id
        ) as operation_seq,
        coalesce((
            select
                max(h.seq)
            from
                history h
            where 1=1
                and h.category = 'compact'
        ), 0) as compact_seq,
        a.seq as assembly_seq,
        a.record_id as assembly_id,
        coalesce(a.payload ->> 'mode', 'conversation') as mode,
        (a.payload ->> 'from_seq')::bigint as from_seq,
        (a.payload ->> 'through_seq')::bigint as through_seq
    from
        cut c
        left join lateral (
            select
                h.seq,
                h.record_id,
                h.payload
            from
                history h
            where 1=1
                and h.category = 'assembly'
                and h.scope = 'request'
                and h.operation_id = c.operation_id
                and h.key = 'context'
            order by
                h.seq desc
            limit
                1
        ) a on true
),
active as materialized (
    select
        h.seq,
        h.record_id,
        h.category,
        h.role,
        h.key,
        h.operation,
        h.scope
    from
        history h
        cross join bounds b
    where 1=1
        and h.seq >= b.compact_seq
        and h.scope is distinct from 'request'
        and h.category <> 'operational'
    union all
    select
        h.seq,
        h.record_id,
        h.category,
        h.role,
        h.key,
        h.operation,
        h.scope
    from
        history h
        cross join bounds b
    where 1=1
        and h.operation_id = b.operation_id
        and h.scope = 'request'
        and h.category <> 'operational'
),
inputs as materialized (
    select
        a.seq,
        a.record_id,
        a.category,
        a.role,
        a.key,
        a.operation,
        a.scope,
        a.scope is not distinct from 'request' as request_local,
        c.section,
        c.windowed,
        c.strict_remove,
        case c.reduction when 'last' then a.key else a.record_id end as group_key
    from
        active a
        left join categories c on c.category = a.category
),
latest as (
    select
        coalesce(
            max(i.seq) filter (
                where 1=1
                    and i.request_local
            ),
            max(i.seq)
        ) as seq
    from
        inputs i
    group by
        i.category, i.group_key
),
selected as materialized (
    select
        s.seq,
        s.record_id,
        s.category,
        s.role,
        s.key,
        s.section,
        h.payload
    from
        latest l
        join inputs s on s.seq = l.seq
        join history h on h.seq = s.seq
        cross join bounds b
    where 1=1
        and s.operation = 'set'
        and (b.mode = 'conversation' or s.request_local or s.category in ('setting', 'tool_call', 'tool_result'))
        and (not s.windowed or s.request_local or s.seq between coalesce(b.from_seq, b.compact_seq) and coalesce(b.through_seq, b.seq))
),
message_inputs as materialized (
    select
        s.seq,
        s.record_id,
        s.role,
        s.section,
        s.payload
    from
        selected s
    where 1=1
        and s.section is not null
),
call_references as materialized (
    select
        m.seq,
        m.record_id as assistant_id,
        p.position,
        p.value ->> 'call_id' as call_id
    from
        message_inputs m
        cross join lateral jsonb_array_elements(m.payload -> 'content') with ordinality p(value, position)
    where 1=1
        and p.value ->> 'kind' = 'tool_call_reference'
),
definitions as materialized (
    select
        s.seq,
        s.record_id,
        s.key as call_id,
        s.payload ->> 'assistant_record_id' as assistant_id,
        s.payload ->> 'name' as name,
        s.payload -> 'arguments' as arguments,
        s.payload ->> 'task_id' as task_id
    from
        selected s
        left join history parent on parent.record_id = s.payload ->> 'assistant_record_id'
        left join message_inputs m on m.record_id = parent.record_id
    where 1=1
        and s.category = 'tool_call'
        and (parent.seq is null or m.seq is not null)
),
calls as materialized (
    select
        p.seq as assistant_seq,
        p.assistant_id,
        p.position,
        coalesce(p.call_id, d.call_id) as call_id,
        d.seq as call_seq,
        d.record_id as call_record_id,
        d.assistant_id as call_assistant_id,
        d.name,
        d.arguments,
        d.task_id
    from
        call_references p
        full join definitions d on d.call_id = p.call_id
),
results as (
    select
        r.seq,
        r.record_id,
        r.key as call_id,
        r.payload
    from
        selected r
        left join history d on d.record_id = r.payload ->> 'call_record_id'
        left join calls c on c.call_id = r.key
    where 1=1
        and r.category = 'tool_result'
        and (c.call_id is not null or d.seq is null or d.category <> 'tool_call')
),
responses as materialized (
    select
        c.assistant_seq,
        c.assistant_id,
        c.position,
        coalesce(c.call_id, r.call_id) as call_id,
        c.call_seq,
        c.call_record_id,
        c.call_assistant_id,
        c.task_id,
        r.seq as result_seq,
        r.record_id as result_id,
        r.payload ->> 'call_record_id' as result_call_record_id,
        r.payload ->> 'status' as status,
        r.payload ->> 'source_record_id' as source_record_id,
        p.seq as source_seq,
        p.category as source_category,
        p.payload ->> 'source_record_id' as source_parent_id,
        jsonb_array_length(r.payload -> 'content') as own_content_count,
        jsonb_typeof(r.payload -> 'state') as own_state_type,
        coalesce(p.payload, r.payload) as payload
    from
        calls c
        full join results r on r.call_id = c.call_id
        left join history p on p.record_id = r.payload ->> 'source_record_id'
),
content_rows as materialized (
    select
        m.seq,
        m.record_id,
        m.role,
        array[m.section::bigint, m.seq, 0] as ordering,
        null::text as call_id,
        m.payload
    from
        message_inputs m
    union all
    select
        r.result_seq,
        r.result_id,
        'tool',
        array[1, r.assistant_seq, r.position],
        r.call_id,
        r.payload
    from
        responses r
    where 1=1
        and r.assistant_seq is not null
        and r.result_seq is not null
),
top_parts as materialized (
    select
        r.seq,
        r.record_id,
        r.role,
        r.ordering,
        array[p.position] as position,
        p.value ->> 'kind' as kind,
        p.value as payload
    from
        content_rows r
        cross join lateral jsonb_array_elements(r.payload -> 'content') with ordinality p(value, position)
),
parts as materialized (
    select
        p.seq,
        p.record_id,
        p.role,
        p.ordering,
        p.position,
        p.kind,
        p.payload
    from
        top_parts p
    union all
    select
        p.seq,
        p.record_id,
        p.role,
        p.ordering,
        p.position || c.position,
        c.value ->> 'kind',
        c.value
    from
        top_parts p
        cross join lateral jsonb_array_elements(nullif(p.payload -> 'content', 'null'::jsonb)) with ordinality c(value, position)
    where 1=1
        and p.kind = 'resource'
),
encoded as (
    select
        p.ordering,
        p.position,
        'content' as field,
        jsonb_build_object(
            'type', 'text',
            'text', coalesce(p.payload -> 'text', p.payload -> 'name')
        ) as value
    from
        parts p
    where 1=1
        and p.kind in ('text', 'tool_reference')
    union all
    select
        p.ordering,
        p.position,
        'content',
        jsonb_build_object(
            'type', 'image_url',
            'image_url', jsonb_build_object(
                'url', 'data:' || (p.payload ->> 'media_type') || ';base64,' || translate(p.payload ->> 'data', '-_', '+/')
            )
        )
    from
        parts p
    where 1=1
        and p.kind = 'image'
    union all
    select
        array[1, c.assistant_seq, 0],
        array[c.position],
        'tool_calls',
        jsonb_build_object(
            'id', c.call_id,
            'type', 'function',
            'function', jsonb_build_object(
                'name', c.name,
                'arguments', c.arguments::text
            )
        )
    from
        calls c
    where 1=1
        and c.assistant_seq is not null
),
fields as (
    select
        e.ordering,
        e.field,
        jsonb_agg(e.value order by e.position) as value
    from
        encoded e
    group by
        e.ordering, e.field
    union all
    select
        p.ordering,
        'reasoning_content',
        to_jsonb(string_agg(p.payload ->> 'text', '' order by p.position))
    from
        parts p
    where 1=1
        and p.kind = 'reasoning'
    group by
        p.ordering
),
messages as materialized (
    select
        r.seq,
        r.record_id,
        r.role,
        r.ordering,
        r.call_id,
        jsonb_build_object('role', r.role, 'content', d.empty_content)
            || coalesce(f.value, '{}'::jsonb)
            || jsonb_strip_nulls(jsonb_build_object('tool_call_id', r.call_id)) as value
    from
        content_rows r
        left join roles d on d.role = r.role
        left join (
            select
                f.ordering,
                jsonb_object_agg(f.field, f.value) as value
            from
                fields f
            group by
                f.ordering
        ) f on f.ordering = r.ordering
),
schemas as materialized (
    select
        s.seq,
        s.record_id,
        s.key,
        s.payload,
        s.payload ->> 'catalog_revision' as catalog_revision,
        a.seq as catalog_seq,
        a.category as catalog_category,
        a.key as catalog_key,
        a.operation as catalog_operation,
        removed.seq as removed_at
    from
        selected s
        left join history a on a.record_id = s.payload ->> 'catalog_revision'
        left join lateral (
            select
                r.seq
            from
                history r
            where 1=1
                and r.category = 'tool_catalog'
                and r.key = s.key
                and r.operation = 'remove'
                and r.seq > a.seq
            order by
                r.seq
            limit
                1
        ) removed on true
    where 1=1
        and s.category = 'tool_schema'
),
request_fields as materialized (
    select
        s.seq,
        s.record_id,
        p.key as name,
        p.value
    from
        selected s
        cross join lateral jsonb_each(s.payload -> 'value') p
    where 1=1
        and s.category = 'setting'
    union all
    select
        null,
        null,
        'messages',
        coalesce(jsonb_agg(m.value order by m.ordering), '[]'::jsonb)
    from
        messages m
    union all
    select
        null,
        null,
        'tools',
        jsonb_agg(jsonb_build_object(
            'type', 'function',
            'function', jsonb_build_object(
                'name', s.key,
                'description', s.payload #> '{tool,description}',
                'parameters', s.payload #> '{tool,parameters}'
            )
        ) order by s.key)
    from
        schemas s
    where 1=1
        and s.removed_at is null
    having
        count(1) > 0
),
removals as (
    select
        i.seq,
        i.record_id,
        i.category,
        i.key,
        i.operation,
        lag(i.operation) over (
            partition by i.category, i.key
            order by i.request_local, i.seq
        ) as previous_operation
    from
        inputs i
    where 1=1
        and i.strict_remove
)
select
    (
        select
            s.payload ->> 'provider'
        from
            selected s
        where 1=1
            and s.category = 'setting'
            and s.key = 'model'
    ) as provider,
    (
        select
            jsonb_object_agg(f.name, f.value order by f.name)
        from
            request_fields f
    ) as request,
    jsonb_build_object(
        'selection', (
            select
                to_jsonb(b)
            from
                bounds b
        ),
        'input_types', (
            select
                coalesce(jsonb_agg(to_jsonb(t) order by t.category, t.scope), '[]'::jsonb)
            from
                (
                    select
                        a.category,
                        a.scope,
                        min(a.seq) as seq
                    from
                        active a
                    group by
                        a.category, a.scope
                ) t
        ),
        'removals', (
            select
                coalesce(jsonb_agg(to_jsonb(r) order by r.seq), '[]'::jsonb)
            from
                removals r
            where 1=1
                and r.operation = 'remove'
        ),
        'schemas', (
            select
                coalesce(jsonb_agg(jsonb_build_object(
                    'seq', s.seq,
                    'record_id', s.record_id,
                    'key', s.key,
                    'catalog_revision', s.catalog_revision,
                    'catalog_seq', s.catalog_seq,
                    'catalog_category', s.catalog_category,
                    'catalog_key', s.catalog_key,
                    'catalog_operation', s.catalog_operation,
                    'removed_at', s.removed_at
                ) order by s.seq), '[]'::jsonb)
            from
                schemas s
        ),
        'calls', (
            select
                coalesce(jsonb_agg(jsonb_build_object(
                    'assistant_seq', r.assistant_seq,
                    'assistant_id', r.assistant_id,
                    'position', r.position,
                    'call_id', r.call_id,
                    'call_seq', r.call_seq,
                    'call_record_id', r.call_record_id,
                    'call_assistant_id', r.call_assistant_id,
                    'task_id', r.task_id,
                    'result_seq', r.result_seq,
                    'result_id', r.result_id,
                    'result_call_record_id', r.result_call_record_id,
                    'status', r.status,
                    'source_record_id', r.source_record_id,
                    'source_seq', r.source_seq,
                    'source_category', r.source_category,
                    'source_parent_id', r.source_parent_id,
                    'own_content_count', r.own_content_count,
                    'own_state_type', r.own_state_type
                ) order by r.assistant_seq, r.position), '[]'::jsonb)
            from
                responses r
        ),
        'content', (
            select
                coalesce(jsonb_agg(jsonb_build_object(
                    'seq', p.seq,
                    'record_id', p.record_id,
                    'role', p.role,
                    'ordering', p.ordering,
                    'position', p.position,
                    'kind', p.kind,
                    'signature_format', p.payload ->> 'format',
                    'content_type', jsonb_typeof(p.payload -> 'content')
                ) order by p.ordering, p.position), '[]'::jsonb)
            from
                parts p
        ),
        'messages', (
            select
                coalesce(jsonb_agg(jsonb_build_object(
                    'seq', m.seq,
                    'record_id', m.record_id,
                    'role', m.role,
                    'ordering', m.ordering,
                    'call_id', m.call_id
                ) order by m.ordering), '[]'::jsonb)
            from
                messages m
        ),
        'parameters', (
            select
                coalesce(jsonb_agg(jsonb_build_object(
                    'seq', f.seq,
                    'record_id', f.record_id,
                    'name', f.name
                ) order by f.name, f.seq), '[]'::jsonb)
            from
                request_fields f
        )
    ) as validation_data;
