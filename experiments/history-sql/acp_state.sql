-- Снимок сохранённых настроек, разрешений и счётчиков для показа при восстановлении.
-- Ручной просмотр: в params подставить 'sql-acp-demo', 25 и выполнить весь файл.
-- Результат: bounds, update с JSON-ресурсом настроек, отдельный replay_complete.
-- Маркер отправляет адаптер один раз после всей истории; SQL ничего не отправляет.
-- Текущее состояние исполнения и редактируемые configOptions здесь не собираются.
-- Подробнее: docs/agent-history-to-acp.md, раздел «Как использовать запросы».
with
params as (
    select
        $1::text as session_id,       -- records.session_id: какой чат читать
        $2::bigint as through_seq     -- снимок по этот seq включительно; null = последний
),
history as not materialized (
    select
        r.*
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
compact as (
    select
        coalesce(max(h.seq), 0) as seq
    from
        history h
        cross join bounds b
    where 1=1
        and h.category = 'compact'
        and h.seq <= b.through_seq
),
settings as (
    select distinct on (h.key)
        h.key,
        h.record_id,
        h.operation,
        h.payload -> 'value' as value,
        h.payload -> 'provider' as provider
    from
        history h
        cross join bounds b
        cross join compact c
    where 1=1
        and h.category = 'setting'
        and h.request_id is null
        and h.seq >= c.seq
        and h.seq <= b.through_seq
    order by
        h.key, h.seq desc
),
permissions as (
    select distinct on (h.key)
        h.key,
        h.record_id,
        h.payload
    from
        history h
        cross join bounds b
    where 1=1
        and h.kind = 'permission_state'
        and h.seq <= b.through_seq
    order by
        h.key, h.seq desc
),
request_usage as (
    select distinct on (h.request_id)
        h.request_id,
        h.payload -> 'usage' as usage
    from
        history h
        cross join bounds b
    where 1=1
        and h.kind = 'token_usage'
        and h.copied_from_id is null
        and h.seq <= b.through_seq
    order by
        h.request_id, h.seq desc
),
usage_totals as (
    select
        v.key,
        sum(v.value::bigint) as value
    from
        request_usage r
        cross join lateral jsonb_each_text(r.usage) v
    group by
        v.key
)
select
    to_jsonb(b) as bounds,
    jsonb_build_object('sessionUpdate', 'replay_complete') as replay_complete,
    jsonb_build_object(
        'sessionUpdate', 'agent_message',
        'messageId', jsonb_build_array('session-state', b.session_id, b.through_seq)::text,
        'content', jsonb_build_array(jsonb_build_object(
            'type', 'resource',
            'resource', jsonb_build_object(
                'uri', 'urn:boba:session-state',
                'mimeType', 'application/json',
                'text', jsonb_build_object(
                    'throughSeq', b.through_seq,
                    'settings', (select coalesce(jsonb_agg(jsonb_build_object(
                        'key', key,
                        'recordId', record_id,
                        'value', value,
                        'provider', provider
                    ) order by key), '[]'::jsonb)
                        from
                            settings
                        where 1=1
                            and operation = 'set'
                    ),
                    'permissions', (
                        select
                            coalesce(jsonb_agg(jsonb_build_object(
                                'key', key,
                                'recordId', record_id,
                                'mode', payload -> 'mode',
                                'rules', payload -> 'rules'
                            ) order by key), '[]'::jsonb)
                        from
                            permissions
                    ),
                    'usage', (
                        select
                            coalesce(
                                jsonb_object_agg(key, value order by key),
                                '{}'::jsonb
                            )
                        from
                            usage_totals
                    )
                )::text
            )
        ))
    ) as update
from
    bounds b;
