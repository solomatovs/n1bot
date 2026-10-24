-- Второй шаг слияния: данные копии базы dev, которую stage.sh положил в
-- целевую базу схемами cutover_aid, cutover_boba, cutover_mcp и cutover_ix,
-- дописываются в таблицы работающего чата, после чего эти схемы удаляются.
-- Скрипт выполняется в целевой базе одной транзакцией; любая ошибка
-- откатывает всё вместе с удалением схем копии.
--
-- Что и как дописывается:
--   * пользователи — по логину (identifier): логин, который в целевой базе
--     уже есть, остаётся своей строкой, и всё, что копия вешает на её
--     пользователя (треды, настройки агента треда, гранты), переходит на id
--     целевой строки;
--   * роли — по имени роли, так же;
--   * треды, оценки ответов и настройки агента треда — по своему id;
--     строка, id которой в целевой базе уже есть, не трогается;
--   * сессии истории агента и их записи — сессия, которой в целевой базе ещё
--     нет, переносится целиком; существующая не трогается, вывод называет
--     число таких сессий;
--   * соединения и их гранты, база знаний (kb_*), описания (node, edge) и
--     индекс ix — группой, только если в целевой базе вся группа пустая:
--     иначе группа пропускается с заметкой. Регистры ix (surface, aspect и
--     прочие) не переносятся: их ведут индексаторы целевой базы, и каждое
--     значение копии обязано в них быть.
--
-- Повторный запуск без новой копии ничего не делает: схем cutover_* нет.
-- Повторное слияние той же копии (stage.sh ещё раз) ничего не дублирует.
--
-- Переменные psql (-v), все необязательные: aid_schema (aid), boba_schema
-- (boba), mcp_schema (mcp), ix_schema (ix) — схемы чата, истории агента,
-- boba-mcp и индекса в целевой базе.
--
-- Запуск:
--   PGDATABASE=<целевая база> psql -f merge.sql

\set ON_ERROR_STOP on
\set QUIET on
\pset pager off

\if :{?aid_schema}
\else
    \set aid_schema aid
\endif
\if :{?boba_schema}
\else
    \set boba_schema boba
\endif
\if :{?mcp_schema}
\else
    \set mcp_schema mcp
\endif
\if :{?ix_schema}
\else
    \set ix_schema ix
\endif

select to_regnamespace('cutover_aid') is null as nothing_staged \gset
\if :nothing_staged
    \echo 'There is no staged schema cutover_aid in this database, so there is nothing to merge: stage a copy with stage.sh first.'
    \quit
\endif

begin;

set local client_min_messages = warning;

-- Таблицы истории в раскладке хранилища агента (boba-history-postgres,
-- schema/10_history.sql): целевая база могла ещё не видеть агента.
create table if not exists :"boba_schema".session (
    id         text        primary key,
    created_at timestamptz not null
);

alter table :"boba_schema".session add column if not exists owner text;

create index if not exists session_owner_id_idx on :"boba_schema".session (owner, id);

create table if not exists :"boba_schema".history (
    session_id  text        not null references :"boba_schema".session on delete cascade,
    seq         integer     not null,
    type        text        not null,
    recorded_at timestamptz not null,
    record      jsonb       not null,
    primary key (session_id, seq)
);

-- Копия строк одной таблицы схемы копии в одноимённую таблицу целевой базы:
-- колонки по имени, значения типов схемы копии (перечисления ix) — через
-- текст в тип целевой схемы, вычисляемые колонки целевой таблицы считает
-- она сама. Колонка копии, которой в целевой таблице нет, остановила бы
-- перенос с потерей данных, поэтому она — ошибка. Последовательности
-- колонок целевой таблицы после копии продолжаются за наибольшим значением.
create function pg_temp.copy_rows(source_schema text, target_schema text, relation text)
returns bigint
language plpgsql
as $$
declare
    lost text;
    columns text;
    selected text;
    copied bigint;
    serial record;
begin
    select
        string_agg(s.column_name, ', ' order by s.ordinal_position)
    into
        lost
    from
        information_schema.columns as s
    where
        s.table_schema = source_schema
        and s.table_name = relation
        and not exists (
            select 1
            from information_schema.columns as t
            where t.table_schema = target_schema
                and t.table_name = relation
                and t.column_name = s.column_name
        );

    if lost is not null then
        raise exception 'merging %.% into %.%: the target table has no columns % of the copy, so their values would be lost', source_schema, relation, target_schema, relation, lost;
    end if;

    select
        string_agg(format('%I', t.column_name), ', ' order by t.ordinal_position),
        string_agg(
            case
                when s.udt_schema = source_schema
                    then format('%I::text::%I.%I', t.column_name, t.udt_schema, t.udt_name)
                else format('%I', t.column_name)
            end,
            ', ' order by t.ordinal_position
        )
    into
        columns,
        selected
    from
        information_schema.columns as t
        join information_schema.columns as s
            on s.table_schema = source_schema
            and s.table_name = t.table_name
            and s.column_name = t.column_name
    where
        t.table_schema = target_schema
        and t.table_name = relation
        and t.is_generated = 'NEVER';

    execute format(
        'insert into %I.%I (%s) overriding system value select %s from %I.%I',
        target_schema, relation, columns, selected, source_schema, relation
    );
    get diagnostics copied = row_count;

    for serial in
        select
            q.oid::regclass::text as sequence,
            a.attname as column_name
        from
            pg_depend as d
            join pg_class as q
                on q.oid = d.objid
                and q.relkind = 'S'
            join pg_attribute as a
                on a.attrelid = d.refobjid
                and a.attnum = d.refobjsubid
        where
            d.refobjid = format('%I.%I', target_schema, relation)::regclass
            and d.deptype in ('a', 'i')
    loop
        execute format(
            'select setval(%L, greatest((select max(%I) from %I.%I), (select last_value from %s)))',
            serial.sequence, serial.column_name, target_schema, relation, serial.sequence
        );
    end loop;

    return copied;
end
$$;

-- Группа таблиц переносится, только если в целевой базе пусты все её
-- таблицы; таблица, которой нет в копии (компонент в dev не стоял),
-- пропускается. Порядок таблиц группы — порядок ссылок между ними.
create temporary table group_result (
    name     text,
    relation text,
    copied   bigint,
    note     text
) on commit drop;

create function pg_temp.copy_group(name text, source_schema text, target_schema text, relations text[])
returns void
language plpgsql
as $$
declare
    occupied text;
    relation text;
begin
    select
        string_agg(r, ', ')
    into
        occupied
    from
        unnest(relations) as r
    where
        to_regclass(format('%I.%I', target_schema, r)) is not null
        and to_regclass(format('%I.%I', source_schema, r)) is not null
        and pg_temp.has_rows(target_schema, r);

    if occupied is not null then
        insert into group_result values (name, null, 0, format('skipped: the target tables %s already have rows', occupied));
        return;
    end if;

    foreach relation in array relations loop
        if to_regclass(format('%I.%I', source_schema, relation)) is null then
            continue;
        end if;

        if to_regclass(format('%I.%I', target_schema, relation)) is null then
            raise exception 'merging group %: the copy has the table %.%, but the target schema % has no such table', name, source_schema, relation, target_schema;
        end if;

        insert into group_result values (name, relation, pg_temp.copy_rows(source_schema, target_schema, relation), null);
    end loop;
end
$$;

create function pg_temp.has_rows(schema_name text, relation text)
returns boolean
language plpgsql
as $$
declare
    found boolean;
begin
    execute format('select exists (select 1 from %I.%I)', schema_name, relation) into found;
    return found;
end
$$;

-- 1. Пользователи и роли: соответствие id копии id целевой базы по логину и
-- имени роли.
create temporary table user_map on commit drop as
select
    c.id as staged_id,
    coalesce(u.id, c.id) as target_id,
    c.identifier,
    u.id is null as added
from
    cutover_aid.users as c
    left join :"aid_schema".users as u
        on u.identifier = c.identifier;

create temporary table taken_users on commit drop as
select
    m.identifier as staged_identifier,
    u.identifier as target_identifier,
    m.staged_id
from
    user_map as m
    join :"aid_schema".users as u
        on u.id = m.staged_id
        and u.identifier <> m.identifier
where
    m.added;

select count(*) > 0 as users_clash from taken_users \gset
\if :users_clash
    \echo 'Users of the copy whose id belongs to another login in the target database:'
    select * from taken_users order by staged_identifier;
    do $$ begin raise exception 'Some user ids of the copy are taken by other logins in the target database (listed above), so nothing was merged.'; end $$;
\endif

insert into :"aid_schema".users (id, identifier, created_at, meta)
select
    c.id,
    c.identifier,
    c.created_at,
    c.meta
from
    cutover_aid.users as c
    join user_map as m
        on m.staged_id = c.id
where
    m.added;

create temporary table role_map on commit drop as
select
    c.id as staged_id,
    coalesce(r.id, c.id) as target_id,
    r.id is null as added
from
    cutover_aid.roles as c
    left join :"aid_schema".roles as r
        on r.role = c.role;

insert into :"aid_schema".roles (id, role, create_at)
select
    c.id,
    c.role,
    c.create_at
from
    cutover_aid.roles as c
    join role_map as m
        on m.staged_id = c.id
where
    m.added
on conflict (id) do nothing;

-- 2. Треды, оценки и настройки агента треда.
create temporary table merged_threads (id uuid) on commit drop;

with inserted as (
    insert into :"aid_schema".threads (id, created_at, name, user_id, tags, meta)
    select
        t.id,
        t.created_at,
        t.name,
        m.target_id,
        t.tags,
        t.meta
    from
        cutover_aid.threads as t
        left join user_map as m
            on m.staged_id = t.user_id
    on conflict (id) do nothing
    returning id
)
insert into merged_threads select id from inserted;

create temporary table merged_feedbacks (id uuid) on commit drop;

with inserted as (
    insert into :"aid_schema".feedbacks (id, for_id, value, thread_id, comment)
    select
        f.id,
        f.for_id,
        f.value,
        f.thread_id,
        f.comment
    from
        cutover_aid.feedbacks as f
    on conflict (id) do nothing
    returning id
)
insert into merged_feedbacks select id from inserted;

create temporary table merged_chat_settings (thread_id uuid) on commit drop;

select to_regclass('cutover_aid.agent_chat_settings') is not null as has_chat_settings \gset
\if :has_chat_settings
    with inserted as (
        insert into :"aid_schema".agent_chat_settings (thread_id, user_id, profile, settings, updated_at)
        select
            s.thread_id,
            m.target_id,
            s.profile,
            s.settings,
            s.updated_at
        from
            cutover_aid.agent_chat_settings as s
            join user_map as m
                on m.staged_id = s.user_id
        on conflict (thread_id) do nothing
        returning thread_id
    )
    insert into merged_chat_settings select thread_id from inserted;
\endif

-- 3. Соединения и гранты — группой: только в базу, где соединений ещё нет.
-- Грант связывает соединение с пользователем или ролью; их id переводятся
-- по соответствию шага 1.
select exists (select 1 from :"aid_schema".connections) as has_connections \gset
\if :has_connections
    insert into group_result values ('connections', null, 0, 'skipped: the target database already has connections');
\else
    with inserted as (
        insert into :"aid_schema".connections (id, name, data)
        select id, name, data from cutover_aid.connections
        returning 1
    )
    insert into group_result
    select 'connections', 'connections', count(*), null from inserted;

    with inserted as (
        insert into :"aid_schema".grants (id, src_kind, src_kind_id, tgt_kind, tgt_kind_id)
        select
            g.id,
            g.src_kind,
            g.src_kind_id,
            g.tgt_kind,
            case g.tgt_kind
                when 'users' then (select m.target_id from user_map as m where m.staged_id = g.tgt_kind_id)
                when 'roles' then (select m.target_id from role_map as m where m.staged_id = g.tgt_kind_id)
            end
        from
            cutover_aid.grants as g
        where
            g.src_kind = 'connections'
        on conflict do nothing
        returning 1
    )
    insert into group_result
    select 'connections', 'grants', count(*), null from inserted;

    create temporary table stray_grants on commit drop as
    select g.* from cutover_aid.grants as g
    where g.src_kind <> 'connections' or g.tgt_kind not in ('users', 'roles');

    select count(*) > 0 as has_stray_grants from stray_grants \gset
    \if :has_stray_grants
        \echo 'Grants of the copy that are not grants of a connection to a user or a role:'
        select * from stray_grants;
        do $$ begin raise exception 'The copy has grants of an unknown kind (listed above), so nothing was merged.'; end $$;
    \endif
\endif

-- 4. История агента: сессия, которой нет в целевой базе, — целиком.
create temporary table kept_sessions on commit drop as
select
    c.id
from
    cutover_boba.session as c
    join :"boba_schema".session as s
        on s.id = c.id;

create temporary table merged_sessions (id text) on commit drop;

with inserted as (
    insert into :"boba_schema".session (id, created_at)
    select
        c.id,
        c.created_at
    from
        cutover_boba.session as c
    where
        not exists (select 1 from kept_sessions as k where k.id = c.id)
    returning id
)
insert into merged_sessions select id from inserted;

create temporary table merged_records (session_id text) on commit drop;

with inserted as (
    insert into :"boba_schema".history (session_id, seq, type, recorded_at, record)
    select
        h.session_id,
        h.seq,
        h.type,
        h.recorded_at,
        h.record
    from
        cutover_boba.history as h
        join merged_sessions as m
            on m.id = h.session_id
    returning session_id
)
insert into merged_records select session_id from inserted;

-- 5. База знаний, описания и индекс — группами.
select pg_temp.copy_group(
    'knowledge base', 'cutover_mcp', :'mcp_schema',
    array['kb_collections', 'kb_sources', 'kb_chunks']
);

select pg_temp.copy_group(
    'descriptions', 'cutover_mcp', :'mcp_schema',
    array['node', 'edge']
);

create temporary table missing_registry on commit drop as
select 'surface' as registry, name::text as value from cutover_ix.surface
except
select 'surface', name::text from :"ix_schema".surface
union all
(
    select 'aspect', aspect::text from cutover_ix.aspect
    except
    select 'aspect', aspect::text from :"ix_schema".aspect
);

select count(*) > 0 as registry_gaps from missing_registry \gset
\if :registry_gaps
    \echo 'Values of the ix registries of the copy that the target registries lack:'
    select * from missing_registry order by registry, value;
    do $$ begin raise exception 'The ix registries of the target database lack values of the copy (listed above), so nothing was merged: upgrade the indexers of the target first.'; end $$;
\endif

select pg_temp.copy_group(
    'ix index', 'cutover_ix', :'ix_schema',
    array[
        'node', 'tree', 'edge',
        'cfl_space', 'cfl_page', 'cfl_blogpost', 'cfl_attachment', 'cfl_comment', 'cfl_page_link',
        'pg_meta_edge', 'pg_meta_database', 'pg_meta_schema', 'pg_meta_table', 'pg_meta_column',
        'pg_meta_view', 'pg_meta_index', 'pg_meta_sequence', 'pg_meta_routine', 'pg_meta_constraint',
        'pg_meta_trigger', 'pg_meta_type', 'pg_meta_statistics',
        'ch_meta_edge', 'ch_meta_server', 'ch_meta_database', 'ch_meta_table', 'ch_meta_view',
        'ch_meta_column', 'ch_meta_index', 'ch_meta_projection', 'ch_meta_dictionary', 'ch_meta_function',
        'ora_meta_edge', 'ora_meta_database', 'ora_meta_schema', 'ora_meta_table', 'ora_meta_view',
        'ora_meta_mview', 'ora_meta_column', 'ora_meta_constraint', 'ora_meta_index', 'ora_meta_sequence',
        'ora_meta_synonym', 'ora_meta_trigger', 'ora_meta_routine',
        'ix_fts', 'ix_trgm', 'ix_emb_e5_1024', 'llm_description'
    ]
);

-- 6. Итог и удаление схем копии.
\echo 'Sessions of the copy that the target database already has; they are left as they are:'
select count(*) as sessions_kept from kept_sessions;

\echo 'Result of the merge (rows of the copy and rows added):'
select 'users' as relation, (select count(*) from cutover_aid.users) as copy_rows, (select count(*) from user_map where added) as added
union all
select 'roles', (select count(*) from cutover_aid.roles), (select count(*) from role_map where added)
union all
select 'threads', (select count(*) from cutover_aid.threads), (select count(*) from merged_threads)
union all
select 'feedbacks', (select count(*) from cutover_aid.feedbacks), (select count(*) from merged_feedbacks)
union all
select 'agent_chat_settings', null, (select count(*) from merged_chat_settings)
union all
select 'sessions', (select count(*) from cutover_boba.session), (select count(*) from merged_sessions)
union all
select 'history records', (select count(*) from cutover_boba.history), (select count(*) from merged_records);

\echo 'Groups:'
select name, relation, copied, note from group_result order by name, relation nulls first;

drop schema cutover_aid, cutover_boba, cutover_mcp, cutover_ix cascade;

commit;

\echo 'The merge is committed; the staged schemas cutover_* are dropped.'
