/*
pg-idx-fts, схема, шаг 0: тип аспектов {schema}.pg_idx_aspect_e и его значения.
Тип общий для индексаторов, каждый создаёт его идемпотентно. Значения отдельным
файлом: использовать их (словарь, предикаты индексов) можно только после коммита.
*/
do $$ begin
    create type {schema}.pg_idx_aspect_e as enum ();
exception when duplicate_object then null; end $$;

alter type {schema}.pg_idx_aspect_e add value if not exists 'meta_description';
alter type {schema}.pg_idx_aspect_e add value if not exists 'meta_comment';
alter type {schema}.pg_idx_aspect_e add value if not exists 'meta_columns';
alter type {schema}.pg_idx_aspect_e add value if not exists 'llm_description';
alter type {schema}.pg_idx_aspect_e add value if not exists 'meta_name';
alter type {schema}.pg_idx_aspect_e add value if not exists 'meta_path';
alter type {schema}.pg_idx_aspect_e add value if not exists 'meta_words';

comment on type {schema}.pg_idx_aspect_e is
    'Какой текст объекта PostgreSQL закодирован в строке поисковой таблицы. Значения только добавляются или переименовываются: на них ссылаются предикаты частичных индексов, и они следуют за переименованием.';
