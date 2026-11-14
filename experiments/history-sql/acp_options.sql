-- Editable parameter descriptions and current values -> native ACP configOptions.
-- All source kinds, paths and allowed values are saved in history, not hardcoded here.
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
        h.seq,
        h.key,
        h.operation,
        h.payload
    from
        history h
        cross join bounds b
    where 1=1
        and h.kind in ('parameter_option', 'parameter_option_removed')
        and h.seq <= b.through_seq
    order by
        h.key, h.seq desc
),
options as (
    select
        seq,
        key,
        payload
    from
        latest
    where 1=1
        and operation = 'set'
),
sources as (
    select
        o.seq,
        o.key,
        o.payload,
        s.operation,
        s.payload #> array(select jsonb_array_elements_text(o.payload -> 'value_path')) as value
    from
        options o
        cross join bounds b
        left join lateral (
            select
                h.operation,
                h.payload
            from
                history h
            where 1=1
                and h.kind = any(array(select jsonb_array_elements_text(o.payload -> 'source_kinds')))
                and h.key is not distinct from o.payload ->> 'source_key'
                and h.request_id is null
                and h.seq <= b.through_seq
            order by
                h.seq desc
            limit
                1
        ) s on true
),
choices as (
    select
        s.seq,
        s.key,
        c.number,
        c.value ->> 'value_id' as value_id,
        c.value ->> 'name' as name,
        c.value -> 'value' = coalesce(v.value, 'null'::jsonb) as current
    from
        sources s
        cross join lateral jsonb_array_elements(s.payload -> 'choices')
            with ordinality c(value, number)
        left join lateral (select s.value) v on s.operation = 'set'
),
selections as (
    select
        seq,
        key,
        count(1) filter (where current) as matches,
        min(value_id) filter (where current) as current_id,
        jsonb_agg(jsonb_build_object('value', value_id, 'name', name) order by number) as choices
    from
        choices
    group by
        seq, key
),
configurations as (
    select
        o.key,
        jsonb_strip_nulls(jsonb_build_object(
            'configId', o.key,
            'name', o.payload -> 'name',
            'description', o.payload -> 'description',
            'type', 'select',
            'currentValue', s.current_id,
            'options', s.choices
        )) as value
    from
        options o
        join selections s on s.key = o.key
)
select
    to_jsonb(b) as bounds,
    jsonb_build_object(
        'sessionUpdate', 'config_option_update',
        'configOptions', (
            select
                coalesce(jsonb_agg(value order by key), '[]'::jsonb)
            from
                configurations
        )
    ) as update,
    (
        select
            coalesce(jsonb_agg(jsonb_build_object(
                'seq', seq, 
                'key', key, 
                'matches', matches
            ) order by key), '[]'::jsonb)
        from
            selections
    ) as selections
from
    bounds b;
