#!/bin/bash
# Среда разработчика на общих серверах стенда. Разработчику заводятся своя база чата и
# сервиса aid_<разработчик> и рабочие базы соединений playground_<разработчик> в postgres и
# ClickHouse. В базу чата копируются пользователи, роли, соединения и гранты из базы
# стенда, а соединения postgres и clickhouse переводятся на рабочие базы разработчика:
# так отладка не пишет ни в базы стенда, ни в базы другого разработчика.
# Повторный запуск ничего не дублирует: готовые базы остаются, соединения копируются
# только в пустую таблицу, перевод соединений повторяется без изменений.
#
# Аргументы: разработчик, база стенда (источник соединений), шаблон базы чата с
# расширениями, кластер ClickHouse.
# Postgres — переменные libpq (PGHOST, PGPORT, PGUSER; пароль — ~/.pgpass), роль с
# CREATEDB. ClickHouse — CH_URL (адрес HTTP-интерфейса) и CH_AUTH: negotiate — билет
# kerberos текущего пользователя, password — CH_USER и CH_PASSWORD из окружения.
set -euo pipefail

if [ "$#" -ne 4 ]; then
    echo "usage: dev-db.sh <developer> <stand database> <template> <clickhouse cluster>" >&2
    exit 2
fi

developer="$1"
source_db="$2"
template="$3"
ch_cluster="$4"

if ! [[ "$developer" =~ ^[a-z][a-z0-9_]*$ ]]; then
    echo "dev-db: developer '$developer' expects a lowercase name of letters, digits and _, starting with a letter, such as the login on the machine" >&2
    exit 2
fi

for pair in "stand database:$source_db" "template:$template" "clickhouse cluster:$ch_cluster"; do
    if [ -z "${pair#*:}" ]; then
        echo "dev-db: the ${pair%%:*} is empty; pass it to the script (compose/dev-db.sh <developer> <stand database> <template> <clickhouse cluster>)" >&2
        exit 2
    fi
done

if ! [[ "$ch_cluster" =~ ^[A-Za-z0-9_]+$ ]]; then
    echo "dev-db: clickhouse cluster '$ch_cluster' expects a name of letters, digits and _, as in remote_servers of the server" >&2
    exit 2
fi

if [ -z "${CH_URL:-}" ]; then
    echo "dev-db: CH_URL is not set; give the HTTP address of ClickHouse, such as https://ch01.loshara.com/" >&2
    exit 2
fi

case "${CH_AUTH:-}" in
    negotiate) ;;
    password)
        if [ -z "${CH_USER:-}" ] || [ -z "${CH_PASSWORD:-}" ]; then
            echo "dev-db: CH_AUTH=password needs CH_USER and CH_PASSWORD in the environment" >&2
            exit 2
        fi
        ;;
    *)
        echo "dev-db: CH_AUTH expects negotiate or password, got '${CH_AUTH:-}'" >&2
        exit 2
        ;;
esac

here=$(cd "$(dirname "$0")" && pwd)
merge="$here/../docs/migrations/2026-10-24-history-merge"
aid="aid_$developer"
playground="playground_$developer"
schema=aid

pg() {
    psql -X -q -v ON_ERROR_STOP=1 "$@"
}

ch() {
    local query="$1"
    if [ "$CH_AUTH" = negotiate ]; then
        curl -sS --fail-with-body --negotiate -u : --data-binary "$query" "$CH_URL"
        return
    fi

    # учётка идёт конфигом curl через stdin, а не аргументом: аргументы видны в списке процессов
    printf 'user = "%s:%s"\n' "$CH_USER" "$CH_PASSWORD" |
        curl -sS --fail-with-body -K - --data-binary "$query" "$CH_URL"
}

database_exists() {
    pg -d postgres -At -v name="$1" <<'SQL'
select count(*) from pg_database where datname = :'name';
SQL
}

echo "== developer environment $developer: postgres ${PGHOST:-<libpq default>}, clickhouse $CH_URL =="

if [ "$(database_exists "$aid")" = 1 ]; then
    echo "    $aid exists, kept as is"
else
    pg -d postgres -v name="$aid" -v template="$template" <<'SQL'
create database :"name" template :"template";
SQL
    echo "    $aid created from $template"
fi

if [ "$(database_exists "$playground")" = 1 ]; then
    echo "    $playground exists, kept as is"
else
    pg -d postgres -v name="$playground" <<'SQL'
create database :"name";
SQL
    echo "    $playground created"
fi

PGDATABASE="$playground" pg -f "$merge/playground.sql"

ch "CREATE DATABASE IF NOT EXISTS $playground ON CLUSTER $ch_cluster" > /dev/null
echo "    clickhouse database $playground is on the cluster $ch_cluster"

table=$(pg -d "$aid" -At -v schema="$schema" <<'SQL'
select to_regclass(format('%I.connections', :'schema')) is not null;
SQL
)

tables=(-t "$schema.users" -t "$schema.roles" -t "$schema.connections" -t "$schema.grants")
if [ "$table" = f ]; then
    pg -d "$aid" -v schema="$schema" <<'SQL'
create schema if not exists :"schema";
SQL
    pg_dump -d "$source_db" --no-owner --no-privileges "${tables[@]}" | pg -d "$aid"
    echo "    users, roles, connections and grants copied from $source_db with their tables"
else
    rows=$(pg -d "$aid" -At -v schema="$schema" <<'SQL'
select count(*) from :"schema".connections;
SQL
)
    if [ "$rows" = 0 ]; then
        pg_dump -d "$source_db" --data-only "${tables[@]}" | pg -d "$aid"
        echo "    users, roles, connections and grants copied from $source_db"
    else
        echo "    $aid already has $rows connections, nothing copied from $source_db"
    fi
fi

pg -d "$aid" -At -v schema="$schema" <<'SQL' |
select name from :"schema".connections
where data ->> 'kind' in ('postgres', 'clickhouse')
order by name;
SQL
while IFS= read -r name; do
    pg -d "$aid" -v aid_schema="$schema" -v repoint="$name" -v dbname="$playground" \
        -f "$merge/connections.sql" < /dev/null
done

echo ">>> environment of $developer is ready: chat and service database $aid, working databases $playground"
echo "    debug overlay .vscode/boba/<app>.toml of the developer: postgres.dbname = \"$aid\""
echo "    password connections reach $playground once pg_hba.conf of the server lets the role in, before the gss rules:"
pg -d "$aid" -At -v schema="$schema" <<'SQL' |
select distinct data -> 'auth' ->> 'user' from :"schema".connections
where data ->> 'kind' = 'postgres' and data -> 'auth' ->> 'method' = 'password'
order by 1;
SQL
while IFS= read -r user; do
    echo "        host     $playground   $user  <network of the chat>    scram-sha-256"
done
