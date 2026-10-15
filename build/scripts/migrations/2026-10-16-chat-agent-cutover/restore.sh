#!/bin/bash
# Второй шаг перехода чата на процесс-агента: копия, снятая dump.sh,
# накатывается в новую пустую базу. Одна транзакция: расширения, схемы, типы
# и функции из prelude.sql, затем таблицы с данными из tables.dump, затем
# сверка числа строк с counts.tsv. Любая ошибка откатывает всё, и база
# остаётся пустой.
#
# Функции без своего search_path (immutable_unaccent базы знаний) зовут
# расширение по короткому имени, а pg_restore грузит данные с пустым
# search_path, и вычисляемая колонка kb_chunks.tsv не нашла бы unaccent.
# На время загрузки такие функции получают search_path схем расширений, после
# загрузки он снимается: определение функции остаётся таким, как в старой базе.
#
# Подключение к новой базе — стандартными переменными libpq (PGHOST, PGPORT,
# PGDATABASE, PGUSER …); пароль — из ~/.pgpass или файла PGPASSFILE. Базу
# заранее создаёт администратор, расширения из extensions.txt должны быть
# доступны серверу (vector — пакетом pgvector).
#
# Вызов: PGDATABASE=<новая база> restore.sh <каталог копии>
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "usage: PGDATABASE=<new database> $0 <directory written by dump.sh>" >&2
    exit 2
fi

dump=$1

if [ -z "${PGDATABASE:-}" ]; then
    echo "PGDATABASE is not set: name the new empty database the copy goes into" >&2
    exit 2
fi

for part in extensions.txt functions.txt prelude.sql tables.dump counts.tsv; do
    if [ ! -f "$dump/$part" ]; then
        echo "$dump/$part is missing: pass the directory written by dump.sh" >&2
        exit 2
    fi
done

taken=$(psql -X -q -At -v ON_ERROR_STOP=1 -c "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace where c.relkind in ('r', 'p', 'S', 'v', 'm') and n.nspname not in ('pg_catalog', 'information_schema') and n.nspname not like 'pg_toast%'")
if [ "$taken" != "0" ]; then
    echo "database $PGDATABASE already has $taken tables, sequences or views: the copy goes into a new empty database" >&2
    exit 1
fi

script() {
    echo '\set ON_ERROR_STOP on'

    while read -r name schema; do
        [ -n "$name" ] || continue
        echo "create extension if not exists \"$name\" schema \"$schema\";"
    done < "$dump/extensions.txt"

    cat "$dump/prelude.sql"

    path=$(cut -d' ' -f2 "$dump/extensions.txt" | sort -u | paste -sd, -)
    while read -r function; do
        [ -n "$function" ] || continue
        echo "alter function $function set search_path = $path;"
    done < "$dump/functions.txt"

    echo '\o /dev/null'
    pg_restore --no-owner --no-privileges -f - "$dump/tables.dump"
    echo '\o'

    echo "set search_path = pg_catalog;"
    while read -r function; do
        [ -n "$function" ] || continue
        echo "alter function $function reset search_path;"
    done < "$dump/functions.txt"

    echo "create temporary table restored_counts (name text, expected bigint, got bigint);"
    while IFS=$'\t' read -r name rows; do
        [ -n "$name" ] || continue
        echo "insert into restored_counts select '$name', $rows, count(*) from \"${name%%.*}\".\"${name#*.}\";"
    done < "$dump/counts.tsv"
    echo "select name, expected, got from restored_counts where expected <> got order by name;"
    echo "do \$\$ begin if exists (select 1 from restored_counts where expected <> got) then raise exception 'The restored tables listed above differ from the copy in the number of rows, so nothing was restored. Was the old database written to during dump.sh? Stop the applications and take the copy again.'; end if; end \$\$;"
    echo "select count(*) as tables_restored, sum(got) as rows_restored from restored_counts;"
}

script | psql -X -q -v ON_ERROR_STOP=1 --single-transaction

echo "the copy in $dump is restored into $PGDATABASE"
