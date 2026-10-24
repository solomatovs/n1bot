#!/bin/bash
# Первый шаг слияния базы dev с базой работающего чата: копия базы dev
# переводится в раскладку нового приложения скриптами перехода
# 2026-10-16-chat-agent-cutover и кладётся в целевую базу схемами cutover_aid,
# cutover_boba, cutover_mcp и cutover_ix. Сливает их с таблицами целевой
# базы merge.sql; сама целевая база на этом шаге получает только эти схемы.
#
# Порядок: dump.sh снимает копию таблиц базы dev (только чтение), копия
# накатывается restore.sh в новую рабочую базу из шаблона с расширениями,
# migrate.sql переводит её (беседы — в историю агента, таблицы — по схемам
# aid, boba, mcp), затем схемы рабочей базы переименовываются в cutover_*, у
# вычисляемых колонок снимается выражение (значения остаются данными, и
# загрузка не зовёт функций схемы, которой в целевой базе нет), схемы
# выгружаются pg_dump и накатываются в целевую базу одной транзакцией.
# Рабочая база после этого удаляется; копии остаются в каталоге.
#
# Подключение — стандартными переменными libpq (PGHOST, PGPORT, PGUSER …);
# все три базы на одном сервере. Создать базу из шаблона и накатить копию с
# расширениями может только администратор.
#
# Вызов:
#   SOURCE=<база dev> STAGE=<имя новой рабочей базы> TARGET=<целевая база> \
#   CHAT_URL=<[agent].public_url чата целевой базы> TEMPLATE=<шаблон с расширениями> \
#   stage.sh <пустой каталог>
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
cutover="$here/../2026-10-16-chat-agent-cutover"

if [ $# -ne 1 ]; then
    echo "usage: SOURCE=<dev database> STAGE=<new staging database> TARGET=<target database> CHAT_URL=<chat [agent].public_url> TEMPLATE=<template with extensions> $0 <empty directory>" >&2
    exit 2
fi

out=$1

for name in SOURCE STAGE TARGET CHAT_URL TEMPLATE; do
    if [ -z "${!name:-}" ]; then
        echo "$name is not set: the staging needs SOURCE, STAGE, TARGET, CHAT_URL and TEMPLATE (see the head of $0)" >&2
        exit 2
    fi
done

mkdir -p "$out"
if [ -n "$(ls -A "$out")" ]; then
    echo "directory $out is not empty: the staging writes its copies into an empty directory" >&2
    exit 2
fi

query() {
    psql -X -q -At -v ON_ERROR_STOP=1 -d "$1" -c "$2"
}

staged=$(query "$TARGET" "select coalesce(string_agg(nspname, ', ' order by nspname), '') from pg_namespace where nspname in ('cutover_aid', 'cutover_boba', 'cutover_mcp', 'cutover_ix')")
if [ -n "$staged" ]; then
    echo "database $TARGET already has the staged schemas $staged of an earlier staging: merge them with merge.sql first" >&2
    exit 1
fi

PGDATABASE=$SOURCE "$cutover/dump.sh" "$out/copy"

createdb -T "$TEMPLATE" "$STAGE"

PGDATABASE=$STAGE "$cutover/restore.sh" "$out/copy"

PGDATABASE=$STAGE psql -X -q -v ON_ERROR_STOP=1 -v chat_url="$CHAT_URL" -f "$cutover/migrate.sql"

psql -X -q -v ON_ERROR_STOP=1 -d "$STAGE" --single-transaction <<'SQL'
alter schema aid rename to cutover_aid;
alter schema boba rename to cutover_boba;
alter schema mcp rename to cutover_mcp;
alter schema ix rename to cutover_ix;

do $$
declare
    generated record;
begin
    for generated in
        select
            table_schema,
            table_name,
            column_name
        from
            information_schema.columns
        where
            table_schema like 'cutover\_%'
            and is_generated = 'ALWAYS'
    loop
        execute format(
            'alter table %I.%I alter column %I drop expression',
            generated.table_schema,
            generated.table_name,
            generated.column_name
        );
    end loop;
end
$$;
SQL

pg_dump -Fc --no-owner --no-privileges -d "$STAGE" \
    -n cutover_aid -n cutover_boba -n cutover_mcp -n cutover_ix -f "$out/cutover.dump"

pg_restore --no-owner --no-privileges --single-transaction --exit-on-error -d "$TARGET" "$out/cutover.dump"

dropdb "$STAGE"

echo "the copy of $SOURCE is staged into $TARGET as the schemas cutover_aid, cutover_boba, cutover_mcp and cutover_ix; merge them with merge.sql"
