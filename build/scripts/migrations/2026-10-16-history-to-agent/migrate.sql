-- Разовый перенос истории чата из чекпойнтов langgraph в историю агента.
--
-- Раньше беседу треда хранил чекпойнтер langgraph: сообщения langchain лежат
-- в его таблицах значением канала messages, сериализованным в msgpack. Теперь
-- беседу хранит агент: по записи на строку в таблице history_records, сессия
-- на тред. Скрипт переводит каждый тред, найденный в чекпойнтах, в сессию
-- агента с тем же id и записывает в threads.meta чата ключ agent_session,
-- по которому чат находит сессию треда. Источник только читается.
--
-- Все три схемы — чекпойнтов, истории агента и чата — должны лежать в одной
-- базе: скрипт читает и пишет их одним соединением, без dblink и fdw.
--
-- Переменные psql (передаются через -v):
--   checkpoints_schema — схема чекпойнтов langgraph, например checkpoints;
--   history_schema     — схема истории агента, как [agent.history].schema
--                        в конфиге чата, например agent_history;
--   chainlit_schema    — схема данных чата с таблицами threads и elements,
--                        например chainlit;
--   chat_url           — адрес чата глазами агента, как [agent].public_url,
--                        например https://chat.example.com/boba; из него
--                        строятся ссылки на вложения вопросов;
--   version            — метка в поле version перенесённых записей, по ней
--                        откат находит перенесённое; по умолчанию
--                        history-migrate-2026-10.
--
-- Запуск:
--   psql -d <база> -v checkpoints_schema=checkpoints -v history_schema=agent_history \
--        -v chainlit_schema=chainlit -v chat_url=https://chat.example.com/boba \
--        -f migrate.sql
--
-- Весь перенос идёт одной транзакцией: проверка, рабочие функции, извлечение,
-- перевод, загрузка, привязка тредов, сверка и удаление рабочей схемы
-- history_migrate. Любая ошибка или несошедшаяся сверка откатывает всё, и
-- база остаётся такой, какой была до запуска. Повторный запуск безопасен:
-- сессию, которая уже есть в истории агента, перенос пропускает, и тред,
-- уже привязанный к своей сессии, не меняет. Откат — rollback.sql рядом.

\set ON_ERROR_STOP on
\set QUIET on
\pset pager off

\set missing false
\if :{?checkpoints_schema}
\else
    \echo 'Pass the schema of the langgraph checkpoints, for example -v checkpoints_schema=checkpoints.'
    \set missing true
\endif
\if :{?history_schema}
\else
    \echo 'Pass the schema of the agent history, for example -v history_schema=agent_history.'
    \set missing true
\endif
\if :{?chainlit_schema}
\else
    \echo 'Pass the schema of the chat data, for example -v chainlit_schema=chainlit.'
    \set missing true
\endif
\if :{?chat_url}
\else
    \echo 'Pass the chat address the agent uses, [agent].public_url, for example -v chat_url=https://chat.example.com/boba.'
    \set missing true
\endif
\if :{?version}
\else
    \set version 'history-migrate-2026-10'
\endif
\if :missing
    do $$ begin raise exception 'The migration needs the psql variables named above, so nothing was changed.'; end $$;
\endif

begin;

-- 1. Проверка.
--
-- Тред читается так же, как его читал чекпойнтер при открытии: последний
-- checkpoint корневого пространства имён (checkpoint_ns пустой, наибольший
-- checkpoint_id), значение канала messages той версии, которую он называет,
-- и ожидающие записи этого канала у того же checkpoint'а. Вложенные
-- пространства имён принадлежат подграфам и в беседу не входят.
--
-- Перенос останавливается, ничего не изменив, если тред нельзя перевести:
-- его id не годится в id сессии агента (от 1 до 128 латинских букв, цифр,
-- «-» и «_»), или сообщения записаны не в msgpack — другого формата разбор
-- не знает. Тред, который чат уже привязал к другой сессии агента, не
-- переносится: беседу в нём уже ведёт агент. Так бывает, если в старом
-- треде задали вопрос до переноса или правили вопрос после него, и такие
-- треды выводятся списком.

create temporary table last_checkpoint on commit drop as
select distinct on (thread_id)
    thread_id,
    checkpoint_ns,
    checkpoint_id,
    checkpoint ->> 'ts' as stamp,
    checkpoint -> 'channel_versions' ->> 'messages' as messages_version
from
    :"checkpoints_schema".checkpoints
where
    checkpoint_ns = ''
order by
    thread_id,
    checkpoint_id desc;

\echo 'Source: threads and checkpoints of the langgraph checkpointer.'
select
    count(distinct thread_id) as threads,
    count(*) as checkpoints,
    count(*) filter (where checkpoint_ns <> '') as nested_checkpoints_ignored
from
    :"checkpoints_schema".checkpoints;

create temporary table refused on commit drop as
select
    thread_id,
    'the thread id does not fit a session id of the agent (1 to 128 letters, digits, - or _)' as reason
from
    last_checkpoint
where
    thread_id !~ '^[A-Za-z0-9_-]{1,128}$'
union all
select
    b.thread_id,
    format('the messages of checkpoint %s are serialized as %s, and the migration reads msgpack only', l.checkpoint_id, b.type)
from
    last_checkpoint as l
    join :"checkpoints_schema".checkpoint_blobs as b
        on b.thread_id = l.thread_id
        and b.checkpoint_ns = l.checkpoint_ns
        and b.channel = 'messages'
        and b.version = l.messages_version
where
    b.type not in ('msgpack', 'empty')
union all
select
    w.thread_id,
    format('pending write %s/%s of checkpoint %s is serialized as %s, and the migration reads msgpack only', w.task_id, w.idx, l.checkpoint_id, w.type)
from
    last_checkpoint as l
    join :"checkpoints_schema".checkpoint_writes as w
        on w.thread_id = l.thread_id
        and w.checkpoint_ns = l.checkpoint_ns
        and w.checkpoint_id = l.checkpoint_id
        and w.channel = 'messages'
where
    w.type is distinct from 'msgpack';

select count(*) > 0 as refused from refused \gset
\if :refused
    \echo 'These threads cannot be migrated:'
    select thread_id, reason from refused order by thread_id;
    do $$ begin raise exception 'Some threads cannot be migrated (listed above), so nothing was changed. Fix or remove them in the checkpoints and run the migration again.'; end $$;
\endif

create temporary table kept_by_chat on commit drop as
select
    l.thread_id,
    t.meta ->> 'agent_session' as session_id
from
    last_checkpoint as l
    join :"chainlit_schema".threads as t
        on t.id::text = l.thread_id
where
    t.meta ->> 'agent_session' <> l.thread_id;

\echo 'Threads the chat already keeps in another agent session; they are not migrated:'
select thread_id, session_id from kept_by_chat order by thread_id;

-- 2. Рабочая схема и её функции.
--
-- Схема history_migrate живёт только внутри этой транзакции: в ней лежат
-- функции разбора, разобранные сообщения и собранные записи. В конце
-- переноса она удаляется. Если схема с таким именем уже есть, перенос
-- останавливается на её создании, чтобы не тронуть чужие объекты.

create schema history_migrate;

-- Функция msgpack_json разбирает значение, которое чекпойнтер langgraph
-- записал с type = 'msgpack', в jsonb. Разбор идёт одним проходом по байтам
-- без рекурсии: открытые массивы и словари лежат на стеке, а текст JSON
-- копится кусками в массиве и в конце превращается в jsonb одним
-- приведением. Сообщение langchain записано расширением msgpack с кодом 5
-- (модель pydantic), его тело — массив [модуль, класс, поля, метод], и
-- функция разворачивает расширение в этот массив. Другие расширения,
-- двоичные строки, NaN и нестроковые ключи словарей она отвергает с номером
-- байта, чтобы ничего не потерялось молча. Символ U+0000 jsonb не хранит,
-- поэтому в строках он заменяется на U+FFFD.
create function history_migrate.msgpack_json(blob bytea)
returns jsonb
language plpgsql
immutable
strict
as $$
declare
    buf     bytea := blob || '\x'::bytea;
    total   integer := length(buf);
    pos     integer := 0;
    parts   text[] := '{}';
    remain  integer[] := '{}';
    size    integer[] := '{}';
    is_map  boolean[] := '{}';
    depth   integer := 0;
    skip_separator boolean := false;
    b       integer;
    k       integer;
    i       integer;
    len     bigint;
    u       numeric;
    expo    integer;
    mant    numeric;
    piece   text;
    raw     bytea;
    nul     integer;
    opened  boolean;
    is_key  boolean;
begin
    loop
        if pos >= total then
            raise exception 'The msgpack value ends early at byte % of %.', pos, total;
        end if;

        is_key := false;
        if depth > 0 and not skip_separator then
            if is_map[depth] then
                is_key := remain[depth] % 2 = 0;
                if not is_key then
                    parts := array_append(parts, ':'::text);
                elsif remain[depth] < size[depth] then
                    parts := array_append(parts, ','::text);
                end if;
            elsif remain[depth] < size[depth] then
                parts := array_append(parts, ','::text);
            end if;
        end if;
        skip_separator := false;

        b := get_byte(buf, pos);
        pos := pos + 1;
        piece := null;
        opened := false;
        len := null;

        if b <= 127 then
            piece := b::text;
        elsif b >= 224 then
            piece := (b - 256)::text;
        elsif b between 128 and 159 or b in (220, 221, 222, 223) then
            if b between 128 and 159 then
                len := b & 15;
            else
                k := 2;
                if b in (221, 223) then
                    k := 4;
                end if;
                len := 0;
                for i in 0 .. k - 1 loop
                    len := len * 256 + get_byte(buf, pos + i);
                end loop;
                pos := pos + k;
            end if;

            if b between 128 and 143 or b in (222, 223) then
                if len = 0 then
                    piece := '{}';
                else
                    parts := array_append(parts, '{'::text);
                    depth := depth + 1;
                    remain[depth] := len * 2;
                    size[depth] := len * 2;
                    is_map[depth] := true;
                    opened := true;
                end if;
            else
                if len = 0 then
                    piece := '[]';
                else
                    parts := array_append(parts, '['::text);
                    depth := depth + 1;
                    remain[depth] := len;
                    size[depth] := len;
                    is_map[depth] := false;
                    opened := true;
                end if;
            end if;
        elsif b between 160 and 191 or b in (217, 218, 219) then
            if b between 160 and 191 then
                len := b & 31;
            else
                k := 1;
                if b = 218 then
                    k := 2;
                elsif b = 219 then
                    k := 4;
                end if;
                len := 0;
                for i in 0 .. k - 1 loop
                    len := len * 256 + get_byte(buf, pos + i);
                end loop;
                pos := pos + k;
            end if;

            raw := substring(buf from pos + 1 for len::integer);
            pos := pos + len::integer;
            nul := position('\x00'::bytea in raw);
            while nul > 0 loop
                raw := overlay(raw placing '\xefbfbd'::bytea from nul for 1);
                nul := position('\x00'::bytea in raw);
            end loop;
            piece := to_json(convert_from(raw, 'UTF8'))::text;
        elsif b = 192 then
            piece := 'null';
        elsif b = 194 then
            piece := 'false';
        elsif b = 195 then
            piece := 'true';
        elsif b between 204 and 211 then
            k := 1 << ((b - 204) % 4);
            u := 0;
            for i in 0 .. k - 1 loop
                u := u * 256 + get_byte(buf, pos + i);
            end loop;
            pos := pos + k;
            if b >= 208 and u >= 2::numeric ^ (8 * k - 1) then
                u := u - 2::numeric ^ (8 * k);
            end if;
            piece := trunc(u)::text;
        elsif b in (202, 203) then
            k := 4;
            if b = 203 then
                k := 8;
            end if;
            u := 0;
            for i in 0 .. k - 1 loop
                u := u * 256 + get_byte(buf, pos + i);
            end loop;
            pos := pos + k;
            if k = 8 then
                expo := (trunc(u / 2::numeric ^ 52) % 2048)::integer;
                mant := u % 2::numeric ^ 52;
                if expo = 2047 then
                    raise exception 'The msgpack value holds NaN or infinity at byte %, and JSON has no such number.', pos - 9;
                end if;
                if expo = 0 then
                    piece := to_json(mant::float8 * power(2::float8, -1074))::text;
                else
                    piece := to_json((mant + 2::numeric ^ 52)::float8 * power(2::float8, expo - 1075))::text;
                end if;
                if u >= 2::numeric ^ 63 then
                    piece := '-' || piece;
                end if;
            else
                expo := (trunc(u / 2::numeric ^ 23) % 256)::integer;
                mant := u % 2::numeric ^ 23;
                if expo = 255 then
                    raise exception 'The msgpack value holds NaN or infinity at byte %, and JSON has no such number.', pos - 5;
                end if;
                if expo = 0 then
                    piece := to_json((mant::float8 * power(2::float8, -149))::float4)::text;
                else
                    piece := to_json(((mant + 2::numeric ^ 23)::float8 * power(2::float8, expo - 150))::float4)::text;
                end if;
                if u >= 2::numeric ^ 31 then
                    piece := '-' || piece;
                end if;
            end if;
        elsif b in (199, 200, 201) then
            k := 1 << (b - 199);
            pos := pos + k;
            if get_byte(buf, pos) <> 5 then
                raise exception 'The msgpack extension % at byte % is not a pydantic model (5), and the chat history has no other extensions.', get_byte(buf, pos), pos;
            end if;
            pos := pos + 1;
            if is_key then
                raise exception 'The msgpack map at byte % uses an extension as a key.', pos;
            end if;
            skip_separator := true;
            continue;
        else
            raise exception 'The msgpack format byte 0x% at byte % is binary, fixext or reserved, and the chat history does not use it.', lpad(to_hex(b), 2, '0'), pos - 1;
        end if;

        if is_key and (piece is null or left(piece, 1) <> '"') then
            raise exception 'The msgpack map at byte % has a key that is not a string.', pos;
        end if;

        if opened then
            continue;
        end if;

        parts := array_append(parts, piece::text);

        loop
            if depth = 0 then
                if pos <> total then
                    raise exception 'The msgpack value is followed by % more byte(s).', total - pos;
                end if;
                return array_to_string(parts, '')::jsonb;
            end if;

            remain[depth] := remain[depth] - 1;
            exit when remain[depth] > 0;

            if is_map[depth] then
                parts := array_append(parts, '}'::text);
            else
                parts := array_append(parts, ']'::text);
            end if;
            depth := depth - 1;
        end loop;
    end loop;
end
$$;

-- Идентификатор записи, который не зависит от запуска: uuid из md5 строки
-- ключа с битами версии 3 и варианта RFC 4122. Ключ складывается из id
-- треда, номера сообщения и места записи, например «<тред>/12/0».
create function history_migrate.uuid_of(key text)
returns text
language sql
immutable
strict
as $$
    select (
        substr(h, 1, 12)
        || '3' || substr(h, 14, 3)
        || to_hex((('x' || substr(h, 17, 1))::bit(4)::integer & 3) | 8)
        || substr(h, 18, 15)
    )::uuid::text
    from (select md5(key) as h) as digest
$$;

-- Строка из 24 знаков для идентификаторов запроса и ответа вида req_… и
-- msg_…, которые агент выдаёт сам; она тоже берётся из md5 ключа.
create function history_migrate.token_of(key text)
returns text
language sql
immutable
strict
as $$
    select left(md5(key), 24)
$$;

-- Имя файла как сегмент пути URL: буквы, цифры и знаки _ . - ~ остаются,
-- остальные байты UTF-8 пишутся как %XX. Так чат кодирует имя в ссылке на
-- файл, например «отчёт 1.pdf» становится %D0%BE…%201.pdf.
create function history_migrate.url_segment(name text)
returns text
language sql
immutable
strict
as $$
    select coalesce(string_agg(
        case
            when ch ~ '^[A-Za-z0-9_.~-]$' then ch
            else upper(regexp_replace(encode(convert_to(ch, 'UTF8'), 'hex'), '(..)', '%\1', 'g'))
        end,
        '' order by n
    ), '')
    from regexp_split_to_table(name, '') with ordinality as chars(ch, n)
$$;

-- Истинность значения поля сообщения так, как её понимает условие if в
-- Python: null, false, 0, пустая строка, пустой массив и пустой словарь
-- ложны, остальное истинно.
create function history_migrate.truthy(value jsonb)
returns boolean
language sql
immutable
as $$
    select value is not null
        and value not in ('null'::jsonb, 'false'::jsonb, '0'::jsonb, '""'::jsonb, '[]'::jsonb, '{}'::jsonb)
$$;

-- Текст сообщения langchain: строка как есть, а у списка блоков — склейка
-- строк и текстовых блоков в их порядке.
create function history_migrate.message_text(content jsonb)
returns text
language sql
immutable
as $$
    select case
        when jsonb_typeof(content) = 'string' then content #>> '{}'
        when jsonb_typeof(content) = 'array' then (
            select
                coalesce(string_agg(
                    case
                        when jsonb_typeof(block) = 'string' then block #>> '{}'
                        when block ->> 'type' = 'text' then coalesce(block ->> 'text', '')
                    end,
                    '' order by ord
                ), '')
            from
                jsonb_array_elements(content) with ordinality as blocks(block, ord)
        )
        else ''
    end
$$;

-- 3. Извлечение.
--
-- Каждая строка таблицы source_messages — одно сообщение langchain треда:
-- его класс, поля модели и номер в треде. Сначала идут сообщения канала,
-- затем ожидающие записи по task_id и idx. Ожидающие записи несут итоги
-- инструментов хода, оборванного до конца узла, и сообщение из них берётся,
-- только если сообщения с тем же id в канале ещё нет.

create table history_migrate.source_messages (
    thread_id text    not null,
    n         integer not null,
    stamp     text    not null,
    class     text    not null,
    message   jsonb   not null,
    primary key (thread_id, n)
);

insert into history_migrate.source_messages (thread_id, n, stamp, class, message)
with migrated as (
    select
        l.*
    from
        last_checkpoint as l
    where
        not exists (select 1 from kept_by_chat as k where k.thread_id = l.thread_id)
),
stored as (
    select
        l.thread_id,
        l.stamp,
        0 as part,
        '' as task_id,
        0 as idx,
        item.ord,
        item.value
    from
        migrated as l
        join :"checkpoints_schema".checkpoint_blobs as b
            on b.thread_id = l.thread_id
            and b.checkpoint_ns = l.checkpoint_ns
            and b.channel = 'messages'
            and b.version = l.messages_version
            and b.type = 'msgpack'
        cross join lateral jsonb_array_elements(history_migrate.msgpack_json(b.blob))
            with ordinality as item(value, ord)
),
written as (
    select
        l.thread_id,
        l.stamp,
        1 as part,
        w.task_id,
        w.idx,
        history_migrate.msgpack_json(w.blob) as value
    from
        migrated as l
        join :"checkpoints_schema".checkpoint_writes as w
            on w.thread_id = l.thread_id
            and w.checkpoint_ns = l.checkpoint_ns
            and w.checkpoint_id = l.checkpoint_id
            and w.channel = 'messages'
),
pending as (
    select
        thread_id,
        stamp,
        part,
        task_id,
        idx,
        item.ord,
        item.value
    from
        written
        cross join lateral jsonb_array_elements(
            case
                when jsonb_typeof(written.value -> 0) = 'string' then jsonb_build_array(written.value)
                else written.value
            end
        ) with ordinality as item(value, ord)
),
messages as (
    select * from stored
    union all
    select
        p.*
    from
        pending as p
    where
        not exists (
            select
                1
            from
                stored as s
            where
                s.thread_id = p.thread_id
                and s.value -> 2 ->> 'id' = p.value -> 2 ->> 'id'
        )
)
select
    thread_id,
    row_number() over (partition by thread_id order by part, task_id, idx, ord) as n,
    stamp,
    value ->> 1 as class,
    value -> 2 as message
from
    messages;

-- 4. Перевод.
--
-- Тред становится сессией агента с тем же id, записи идут по seq с 1 и
-- связаны цепочкой parentUuid. Вопрос человека даёт запись user с origin
-- human, и её uuid равен id сообщения, потому что по нему чат привязывает
-- оценки. Ответ модели даёт по записи assistant на каждый блок (рассуждение,
-- текст, вызов инструмента) с общими message.id и requestId; модель берётся
-- последняя названная в треде, а у подготовки хода стоит <synthetic>. Ответ
-- с пометкой error становится синтетической записью об ошибке модели, ответ
-- с пометкой stopped — оборванным ответом и маркером прерывания за ним.
-- Итог инструмента даёт запись user с блоком tool_result, а его артефакт,
-- адрес журнала и типы содержимого уходят в toolUseResult. Время всех
-- записей треда — время его последнего checkpoint'а, потому что своего
-- времени у сообщений нет. Ключи со значением null в документ не пишутся,
-- кроме parentUuid и stop_sequence: так записи пишет сам агент.
--
-- Вложение вопроса чат записывал в additional_kwargs.attachments именем и
-- путём файла в песочнице. Маршрут canvas чата отдаёт файл workspace по
-- треду, каталогу (upload или mermaid) и имени, поэтому ссылку получает
-- только файл, путь которого кончается на <id этого треда>/upload/<имя> или
-- <id этого треда>/mermaid/<имя>: она ведёт на <chat_url>/canvas/ с тем же
-- местом, а mimeType берётся из строки elements этого вопроса. Файл вне
-- каталогов треда, например /workspace/upload/<id> старой раскладки,
-- маршрут не отдаст, поэтому ссылки у него нет: его имя остаётся в тексте
-- вопроса в виде [@имя].

create table history_migrate.units (
    thread_id    text    not null,
    n            integer not null,
    sub          integer not null,
    type         text    not null,
    uuid         text    not null,
    call_id      text,
    answers_call text,
    body         jsonb   not null,
    primary key (thread_id, n, sub)
);

insert into history_migrate.units
with message as (
    select
        s.thread_id,
        s.n,
        s.class,
        s.message as m,
        coalesce(s.message -> 'additional_kwargs', '{}'::jsonb) as extra,
        history_migrate.message_text(s.message -> 'content') as body_text
    from
        history_migrate.source_messages as s
),
reply as (
    select
        message.*,
        coalesce(nullif(m ->> 'id', ''), 'msg_' || history_migrate.token_of(thread_id || '/' || n || '/message')) as message_id,
        'req_' || history_migrate.token_of(thread_id || '/' || n || '/request') as request_id,
        jsonb_build_object(
            'input_tokens', coalesce((m -> 'usage_metadata' ->> 'input_tokens')::integer, 0),
            'output_tokens', coalesce((m -> 'usage_metadata' ->> 'output_tokens')::integer, 0),
            'cache_creation_input_tokens', 0,
            'cache_read_input_tokens', 0
        ) as usage,
        case
            when history_migrate.truthy(extra -> 'error') then 'error'
            when history_migrate.truthy(extra -> 'stopped') then 'stopped'
            else 'reply'
        end as shape
    from
        message
    where
        class = 'AIMessage'
),
block as (
    select
        r.thread_id,
        r.n,
        0 as slot,
        jsonb_build_object('type', 'thinking', 'thinking', r.extra ->> 'reasoning_content', 'signature', '') as content,
        null::text as call_id
    from
        reply as r
    where
        r.shape <> 'error'
        and history_migrate.truthy(r.extra -> 'reasoning_content')
    union all
    select
        r.thread_id,
        r.n,
        1,
        jsonb_build_object('type', 'text', 'text', r.body_text),
        null
    from
        reply as r
    where
        r.shape <> 'error'
        and r.body_text <> ''
    union all
    select
        r.thread_id,
        r.n,
        1 + call.ord::integer,
        jsonb_build_object(
            'type', 'tool_use',
            'id', call.value ->> 'id',
            'name', call.value ->> 'name',
            'input', jsonb_strip_nulls(
                case
                    when jsonb_typeof(call.value -> 'args') = 'object' then call.value -> 'args'
                    else '{}'::jsonb
                end
            )
        ),
        call.value ->> 'id'
    from
        reply as r
        cross join lateral jsonb_array_elements(coalesce(r.m -> 'tool_calls', '[]'::jsonb))
            with ordinality as call(value, ord)
    where
        r.shape <> 'error'
        and history_migrate.truthy(call.value -> 'id')
        and history_migrate.truthy(call.value -> 'name')
),
indexed_block as (
    select
        block.*,
        (row_number() over (partition by thread_id, n order by slot) - 1)::integer as api_block_index
    from
        block
),
named_model as (
    select
        r.thread_id,
        r.n,
        nullif(r.m -> 'response_metadata' ->> 'model_name', '') as model_name
    from
        reply as r
    where
        r.shape = 'reply'
        and not (r.extra ? 'prefetch_elapsed_ms')
        and exists (select 1 from block as b where b.thread_id = r.thread_id and b.n = r.n)
),
carried_model as (
    select
        thread_id,
        n,
        coalesce(
            first_value(model_name) over (partition by thread_id, seen order by n),
            '<unknown>'
        ) as model
    from (
        select
            named_model.*,
            count(model_name) over (partition by thread_id order by n) as seen
        from
            named_model
    ) as counted
),
reply_unit as (
    select
        r.thread_id,
        r.n,
        b.api_block_index as sub,
        'assistant' as type,
        history_migrate.uuid_of(r.thread_id || '/' || r.n || '/' || b.api_block_index) as uuid,
        b.call_id,
        null::text as answers_call,
        jsonb_build_object(
            'type', 'assistant',
            'message', jsonb_build_object(
                'id', r.message_id,
                'type', 'message',
                'role', 'assistant',
                'model', case
                    when r.shape = 'stopped' then '<synthetic>'
                    when r.extra ? 'prefetch_elapsed_ms' then '<synthetic>'
                    else cm.model
                end,
                'content', jsonb_build_array(b.content),
                'stop_reason', case
                    when r.shape = 'reply' and jsonb_array_length(coalesce(r.m -> 'tool_calls', '[]'::jsonb)) > 0 then 'tool_use'
                    else 'end_turn'
                end,
                'stop_sequence', null,
                'usage', r.usage
            ),
            'apiBlockIndex', b.api_block_index,
            'requestId', r.request_id
        ) || case
            when r.shape = 'stopped' then '{"isAbortedMidStream": true}'::jsonb
            else '{}'::jsonb
        end as body
    from
        reply as r
        join indexed_block as b
            on b.thread_id = r.thread_id
            and b.n = r.n
        left join carried_model as cm
            on cm.thread_id = r.thread_id
            and cm.n = r.n
    where
        r.shape <> 'error'
),
failure_unit as (
    select
        r.thread_id,
        r.n,
        0 as sub,
        'assistant' as type,
        history_migrate.uuid_of(r.thread_id || '/' || r.n || '/error') as uuid,
        null::text as call_id,
        null::text as answers_call,
        jsonb_build_object(
            'type', 'assistant',
            'message', jsonb_build_object(
                'id', r.message_id,
                'type', 'message',
                'role', 'assistant',
                'model', '<synthetic>',
                'content', jsonb_build_array(jsonb_build_object('type', 'text', 'text', r.body_text)),
                'stop_reason', 'stop_sequence',
                'stop_sequence', null,
                'usage', '{"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}'::jsonb
            ),
            'apiBlockIndex', 0,
            'requestId', r.request_id,
            'isApiErrorMessage', true,
            'error', r.body_text
        ) as body
    from
        reply as r
    where
        r.shape = 'error'
),
interrupt_unit as (
    select
        r.thread_id,
        r.n,
        1000 as sub,
        'user' as type,
        history_migrate.uuid_of(r.thread_id || '/' || r.n || '/interrupted') as uuid,
        null::text as call_id,
        null::text as answers_call,
        jsonb_build_object(
            'type', 'user',
            'message', jsonb_build_object('role', 'user', 'content', '[Request interrupted by user]')
        ) as body
    from
        reply as r
    where
        r.shape = 'stopped'
),
attached as (
    select
        q.thread_id,
        q.n,
        item.ord,
        item.value ->> 'name' as name,
        regexp_match(item.value ->> 'path', '/([^/]+)/(upload|mermaid)/([^/]+)$') as place,
        (
            select
                e.mime
            from
                :"chainlit_schema".elements as e
            where
                e.for_id::text = q.m ->> 'id'
                and e.name = item.value ->> 'name'
            order by
                e.id
            limit 1
        ) as mime
    from
        message as q
        cross join lateral jsonb_array_elements(
            case
                when jsonb_typeof(q.extra -> 'attachments') = 'array' then q.extra -> 'attachments'
                else '[]'::jsonb
            end
        ) with ordinality as item(value, ord)
    where
        q.class = 'HumanMessage'
        and jsonb_typeof(item.value) = 'object'
        and item.value ->> 'name' is not null
        and item.value ->> 'path' is not null
),
question_attachments as (
    select
        a.thread_id,
        a.n,
        jsonb_agg(
            jsonb_strip_nulls(jsonb_build_object(
                'type', 'resource_link',
                'name', a.name,
                'uri', rtrim(:'chat_url', '/') || '/canvas/' || a.thread_id || '/' || a.place[2] || '/'
                    || history_migrate.url_segment(a.place[3]),
                'mimeType', a.mime
            ))
            order by a.ord
        ) filter (where a.place[1] = a.thread_id) as links,
        string_agg('[@' || a.name || ']', '' order by a.ord) filter (where a.place[1] is distinct from a.thread_id) as unlinked
    from
        attached as a
    group by
        a.thread_id,
        a.n
),
question as (
    select
        q.*,
        q.body_text || coalesce(qa.unlinked, '') as text,
        qa.links
    from
        message as q
        left join question_attachments as qa
            on qa.thread_id = q.thread_id
            and qa.n = q.n
    where
        q.class = 'HumanMessage'
),
question_unit as (
    select
        q.thread_id,
        q.n,
        0 as sub,
        'user' as type,
        case
            when nullif(q.m ->> 'id', '') is null then history_migrate.uuid_of(q.thread_id || '/' || q.n || '/question')
            when row_number() over (partition by q.thread_id, q.m ->> 'id' order by q.n) > 1 then history_migrate.uuid_of(q.thread_id || '/' || q.n || '/question')
            else q.m ->> 'id'
        end as uuid,
        null::text as call_id,
        null::text as answers_call,
        jsonb_build_object(
            'type', 'user',
            'message', jsonb_build_object(
                'role', 'user',
                'content', case
                    when q.links is null then to_jsonb(q.text)
                    else jsonb_build_array(jsonb_build_object('type', 'text', 'text', q.text)) || q.links
                end
            ),
            'origin', '{"kind": "human"}'::jsonb
        ) as body
    from
        question as q
),
tool_unit as (
    select
        t.thread_id,
        t.n,
        0 as sub,
        'user' as type,
        history_migrate.uuid_of(t.thread_id || '/' || t.n || '/result') as uuid,
        null::text as call_id,
        t.m ->> 'tool_call_id' as answers_call,
        jsonb_build_object(
            'type', 'user',
            'message', jsonb_build_object(
                'role', 'user',
                'content', jsonb_build_array(
                    jsonb_build_object(
                        'type', 'tool_result',
                        'tool_use_id', t.m ->> 'tool_call_id',
                        'content', t.body_text
                    ) || case
                        when t.m ->> 'status' = 'error' then '{"is_error": true}'::jsonb
                        else '{}'::jsonb
                    end
                )
            )
        ) || coalesce(
            (
                select
                    jsonb_build_object('toolUseResult', jsonb_strip_nulls(jsonb_object_agg(key, value)))
                from (
                    values
                        ('artifact', t.m -> 'artifact'),
                        ('journal', t.m -> 'response_metadata' -> 'boba_journal'),
                        ('media', t.m -> 'response_metadata' -> 'boba_call_media')
                ) as parts(key, value)
                where
                    value is not null
                    and value <> 'null'::jsonb
                having
                    count(*) > 0
            ),
            '{}'::jsonb
        ) as body
    from
        message as t
    where
        t.class = 'ToolMessage'
)
select * from question_unit
union all
select * from reply_unit
union all
select * from failure_unit
union all
select * from interrupt_unit
union all
select * from tool_unit;

create table history_migrate.records (
    session_id text    not null,
    seq        integer not null,
    type       text    not null,
    record     jsonb   not null,
    primary key (session_id, seq)
);

insert into history_migrate.records (session_id, seq, type, record)
with ordered as (
    select
        u.*,
        row_number() over (partition by thread_id order by n, sub)::integer as seq,
        lag(uuid) over (partition by thread_id order by n, sub) as parent_uuid
    from
        history_migrate.units as u
),
sourced as (
    select
        o.*,
        (
            select
                c.uuid
            from
                ordered as c
            where
                c.thread_id = o.thread_id
                and c.call_id = o.answers_call
                and c.seq < o.seq
            order by
                c.seq desc
            limit 1
        ) as source_uuid
    from
        ordered as o
),
stamped as (
    select distinct on (thread_id)
        thread_id,
        to_char((stamp::timestamptz) at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') as ts
    from
        history_migrate.source_messages
    order by
        thread_id
)
select
    s.thread_id,
    s.seq,
    s.type,
    jsonb_build_object(
        'uuid', s.uuid,
        'parentUuid', s.parent_uuid,
        'timestamp', m.ts,
        'sessionId', s.thread_id,
        'isSidechain', false,
        'version', :'version',
        'seq', s.seq
    )
    || s.body
    || case
        when s.source_uuid is not null then jsonb_build_object('sourceToolAssistantUUID', s.source_uuid)
        else '{}'::jsonb
    end
from
    sourced as s
    join stamped as m
        on m.thread_id = s.thread_id;

-- Заметки перевода: сообщения, которые не стали записями, и вложения без
-- ссылки. Перенос они не останавливают, их выводит сверка.
create table history_migrate.notes (
    thread_id text not null,
    n         integer not null,
    note      text not null
);

insert into history_migrate.notes (thread_id, n, note)
select
    s.thread_id,
    s.n,
    format('Message %s of class %s has no record kind and is skipped.', s.message ->> 'id', s.class)
from
    history_migrate.source_messages as s
where
    s.class not in ('HumanMessage', 'AIMessage', 'ToolMessage')
union all
select
    s.thread_id,
    s.n,
    format('Reply %s has no text, reasoning or calls and is skipped.', s.message ->> 'id')
from
    history_migrate.source_messages as s
where
    s.class = 'AIMessage'
    and not exists (select 1 from history_migrate.units as u where u.thread_id = s.thread_id and u.n = s.n)
union all
select
    s.thread_id,
    s.n,
    format('Tool call %s of reply %s lacks an id or a name and is dropped.', call.value, s.message ->> 'id')
from
    history_migrate.source_messages as s
    cross join lateral jsonb_array_elements(coalesce(s.message -> 'tool_calls', '[]'::jsonb)) as call(value)
where
    s.class = 'AIMessage'
    and not (history_migrate.truthy(call.value -> 'id') and history_migrate.truthy(call.value -> 'name'))
union all
select
    s.thread_id,
    s.n,
    case
        when jsonb_typeof(item.value) <> 'object' or item.value ->> 'name' is null or item.value ->> 'path' is null then
            format('Attachment entry %s of question %s lacks a name or a path and is dropped.', item.value, s.message ->> 'id')
        else
            format('Attachment %s of question %s is not in a directory of its thread, so the chat cannot serve it; the question keeps its name as text without a link.', item.value ->> 'path', s.message ->> 'id')
    end
from
    history_migrate.source_messages as s
    cross join lateral jsonb_array_elements(
        case
            when jsonb_typeof(s.message -> 'additional_kwargs' -> 'attachments') = 'array' then s.message -> 'additional_kwargs' -> 'attachments'
            else '[]'::jsonb
        end
    ) as item(value)
where
    s.class = 'HumanMessage'
    and (
        jsonb_typeof(item.value) <> 'object'
        or item.value ->> 'name' is null
        or item.value ->> 'path' is null
        or (regexp_match(item.value ->> 'path', '/([^/]+)/(upload|mermaid)/[^/]+$'))[1] is distinct from s.thread_id
    )
union all
select
    r.session_id,
    r.seq,
    format('Tool result for call %s has no record of its call before it.', r.record -> 'message' -> 'content' -> 0 ->> 'tool_use_id')
from
    history_migrate.records as r
where
    r.record -> 'message' -> 'content' -> 0 ->> 'type' = 'tool_result'
    and not (r.record ? 'sourceToolAssistantUUID');

-- 5. Загрузка.
--
-- Схема и таблица истории создаются так же, как их создаёт хранилище агента
-- (PostgresHistoryStore): те же колонки, ключ и комментарии, поэтому агент
-- примет раскладку как свою. Переносится только сессия, у которой в истории
-- ещё нет ни одной записи: так повторный запуск ничего не дублирует, а
-- сессию, которую агент уже ведёт, перенос не трогает.

create schema if not exists :"history_schema";

create table if not exists :"history_schema".history_records (
    session_id text        not null,
    seq        integer     not null,
    type       text        not null,
    record     jsonb       not null,
    created_at timestamptz not null default now(),
    primary key (session_id, seq)
);

comment on table :"history_schema".history_records is
    'Журнал сессий агента: по записи на строку, в порядке seq внутри сессии; документ записи такой же, как строка журнала JSONL';

comment on column :"history_schema".history_records.seq is
    'Порядковый номер записи в сессии с 1; выдаёт сессия агента';

comment on column :"history_schema".history_records.type is
    'Вид записи: user, assistant, attachment, system, queue-operation, permission-mode, permission-rule, api-request';

create table history_migrate.loaded (
    session_id text    primary key,
    records    integer not null
);

insert into history_migrate.loaded (session_id, records)
select
    r.session_id,
    count(*)
from
    history_migrate.records as r
where
    not exists (
        select
            1
        from
            :"history_schema".history_records as h
        where
            h.session_id = r.session_id
    )
group by
    r.session_id;

insert into :"history_schema".history_records (session_id, seq, type, record)
select
    r.session_id,
    r.seq,
    r.type,
    r.record
from
    history_migrate.records as r
    join history_migrate.loaded as l
        on l.session_id = r.session_id
order by
    r.session_id,
    r.seq;

-- 6. Привязка тредов.
--
-- Чат находит сессию агента треда только по ключу agent_session в
-- threads.meta. Каждому переведённому треду, у которого есть строка в
-- threads, ключ записывается явно, со значением id самого треда. Тред без
-- строки в threads чат не покажет, но его сессия всё равно переносится.

create table history_migrate.bound (
    thread_id uuid primary key
);

with changed as (
    update
        :"chainlit_schema".threads as t
    set
        meta = case
            when jsonb_typeof(t.meta) = 'object' then t.meta
            else '{}'::jsonb
        end || jsonb_build_object('agent_session', t.id::text)
    where
        t.id::text in (select distinct thread_id from history_migrate.source_messages)
        and t.meta ->> 'agent_session' is distinct from t.id::text
    returning
        t.id
)
insert into history_migrate.bound (thread_id)
select id from changed;

-- 7. Сверка.
--
-- Каждая проверка ниже ищет нарушение, которого при верном переносе быть
-- не может: вопрос или итог инструмента без своей записи, документ в истории,
-- отличный от собранного, seq не подряд, parentUuid не на предыдущую запись,
-- sessionId или seq документа не те, что в колонках. Если найдено хоть одно,
-- перенос откатывается целиком. Заметки перевода и треды без строки в threads
-- только выводятся.

create table history_migrate.problems (
    session_id text not null,
    seq        integer,
    problem    text not null
);

insert into history_migrate.problems (session_id, seq, problem)
select
    s.thread_id,
    null::integer,
    format('%s questions became %s question records.', s.questions, coalesce(r.questions, 0))
from (
    select thread_id, count(*) as questions
    from history_migrate.source_messages
    where class = 'HumanMessage'
    group by thread_id
) as s
left join (
    select session_id, count(*) as questions
    from history_migrate.records
    where record -> 'origin' ->> 'kind' = 'human'
    group by session_id
) as r
    on r.session_id = s.thread_id
where
    s.questions <> coalesce(r.questions, 0)
union all
select
    s.thread_id,
    null,
    format('%s tool results became %s tool result records.', s.results, coalesce(r.results, 0))
from (
    select thread_id, count(*) as results
    from history_migrate.source_messages
    where class = 'ToolMessage'
    group by thread_id
) as s
left join (
    select session_id, count(*) as results
    from history_migrate.records
    where record -> 'message' -> 'content' -> 0 ->> 'type' = 'tool_result'
    group by session_id
) as r
    on r.session_id = s.thread_id
where
    s.results <> coalesce(r.results, 0)
union all
select
    l.session_id,
    null,
    format('The history holds %s records of the session, %s of them different from the %s built ones.', count(h.seq), count(h.seq) filter (where h.record <> r.record), l.records)
from
    history_migrate.loaded as l
    left join :"history_schema".history_records as h
        on h.session_id = l.session_id
    left join history_migrate.records as r
        on r.session_id = h.session_id
        and r.seq = h.seq
group by
    l.session_id,
    l.records
having
    count(h.seq) <> l.records
    or count(h.seq) filter (where h.record is distinct from r.record) > 0
union all
select
    session_id,
    seq,
    problem
from (
    select
        h.session_id,
        h.seq,
        case
            when h.seq <> row_number() over w then 'The seq of the record does not follow the previous one.'
            when h.record ->> 'seq' <> h.seq::text then 'The seq inside the record differs from the seq column.'
            when h.record ->> 'sessionId' <> h.session_id then 'The sessionId inside the record differs from the session_id column.'
            when h.record ->> 'parentUuid' is distinct from lag(h.record ->> 'uuid') over w then 'The parentUuid of the record does not name the previous record.'
        end as problem
    from
        :"history_schema".history_records as h
        join history_migrate.loaded as l
            on l.session_id = h.session_id
    window w as (partition by h.session_id order by h.seq)
) as checked
where
    problem is not null;

\echo 'Notes of the translation (they do not stop the migration):'
select thread_id, n, note from history_migrate.notes order by thread_id, n;

\echo 'Migrated threads without a row in threads (the chat does not list them):'
select
    count(*) as threads_without_chat_row
from
    (select distinct thread_id from history_migrate.source_messages) as s
where
    not exists (select 1 from :"chainlit_schema".threads as t where t.id::text = s.thread_id);

\echo 'Result of the migration:'
select
    (select count(distinct thread_id) from history_migrate.source_messages) as threads_translated,
    (select count(*) from history_migrate.loaded) as sessions_loaded,
    (select coalesce(sum(records), 0) from history_migrate.loaded) as records_loaded,
    (select count(distinct thread_id) from history_migrate.source_messages)
        - (select count(*) from history_migrate.loaded) as sessions_already_in_history,
    (select count(*) from kept_by_chat) as threads_kept_by_chat,
    (select count(*) from history_migrate.bound) as threads_bound,
    :'version' as version;

select count(*) > 0 as failed from history_migrate.problems \gset
\if :failed
    \echo 'The check found problems:'
    select session_id, seq, problem from history_migrate.problems order by session_id, seq;
    do $$ begin raise exception 'The check of the migration found problems (listed above), so nothing was changed.'; end $$;
\endif

-- 8. Удаление рабочей схемы и фиксация.

set local client_min_messages = warning;

drop schema history_migrate cascade;

commit;

\echo 'The migration is committed.'
