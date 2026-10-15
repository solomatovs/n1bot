#!/bin/bash
# Первый шаг перехода чата на процесс-агента: копия таблиц приложения со
# старой базы. Таблицы перечислены поштучно, целые схемы не копируются:
# что приложению не нужно (журналы workflow, mcp.users, тестовые таблицы
# public), в копию не попадает.
#
# Откуда список (кто пользуется таблицей в коде ветки):
#   схема чата ([data_layer].schema, она же [connections].db_schema чата):
#     users, threads — boba-runtime UsersTable и ThreadsTable;
#     feedbacks, elements — boba-runtime FeedbacksTable и ElementsTable;
#     agent_user_settings, agent_chat_settings — boba-chainlit AgentSettingsTable;
#     connections, roles, grants — boba-connection-broker ConnectionStore;
#   схема истории агента ([agent.history].schema): history_records —
#     boba-history-postgres PostgresHistoryStore (есть, если агент уже работал);
#   схема чекпойнтов langgraph: checkpoints, checkpoint_blobs, checkpoint_writes —
#     источник перевода беседы в migrate.sql;
#   схема базы знаний ([tool.kb]): kb_collections, kb_chunks, kb_sources и
#     функция immutable_unaccent — boba-db-pgvector KbSchema;
#   схема описаний ([tool.describer]): node, edge — boba-tool-describer;
#   схема ix: surface, aspect, surface_aspect, index_table, surface_url,
#     surface_prompt, node, tree, edge и типы surface_e, aspect_class_e,
#     aspect_e, index_kind_e — ix-core; cfl_* — cfl-indexer; pg_meta_* —
#     pg-meta-scraper; ch_meta_* — ch-meta-scraper; ora_meta_* —
#     ora-meta-scraper; ix_fts, ix_trgm, ix_emb_e5_1024, llm_description —
#     ix-fts, ix-trgm, ix-vector, ix-llm-describer;
#   схема studio ([automation].schema, она же [connections].db_schema studio):
#     users, connections, roles, grants, connection_syncs, connection_versions —
#     boba-runtime, boba-connection-broker, boba-catalog-service; processes,
#     groups, nodes, flows, process_versions, drafts, draft_ops, shares,
#     process_upgrade_runs, process_upgrades — boba-catalog-service ProcessStore;
#   схема каталога studio ([catalog].db_schema): pg_*, ch_*, link —
#     boba-db-postgres каталог снимков.
# Таблицы шины (схема [messaging].schema: live_*) не копируются: это
# нежурналируемые таблицы с состоянием работающих процессов, приложение
# создаёт их при старте.
#
# Таблица, которой в базе нет (компонент не развёрнут), пропускается с
# сообщением. Последовательности и типы, нужные таблицам, тоже названы явно;
# схемы, типы и функции сохраняются в prelude.sql, таблицы с данными — в
# tables.dump, расширения — в extensions.txt, число строк каждой таблицы —
# в counts.tsv (по нему restore.sh сверяет копию).
#
# Подключение к старой базе — стандартными переменными libpq (PGHOST,
# PGPORT, PGDATABASE, PGUSER, PGSSLMODE …); пароль — из ~/.pgpass или файла
# PGPASSFILE, Kerberos — из кэша билетов. Все запросы идут в транзакциях
# только для чтения. Имена схем, если они у вас другие, задаются переменными
# окружения ниже. Приложение и индексаторы на время копии нужно остановить.
#
# Вызов: PGDATABASE=<старая база> dump.sh <пустой каталог для копии>
set -euo pipefail

CHAT_SCHEMA=${CHAT_SCHEMA:-chainlit}
HISTORY_SCHEMA=${HISTORY_SCHEMA:-agent_history}
CHECKPOINTS_SCHEMA=${CHECKPOINTS_SCHEMA:-checkpoints}
KB_SCHEMA=${KB_SCHEMA:-kb}
DESCRIBER_SCHEMA=${DESCRIBER_SCHEMA:-describer}
IX_SCHEMA=${IX_SCHEMA:-ix}
STUDIO_SCHEMA=${STUDIO_SCHEMA:-automation}
CATALOG_SCHEMA=${CATALOG_SCHEMA:-catalog}

if [ $# -ne 1 ]; then
    echo "usage: PGDATABASE=<old database> $0 <empty directory for the copy>" >&2
    exit 2
fi

out=$1

if [ -z "${PGDATABASE:-}" ]; then
    echo "PGDATABASE is not set: name the old database the copy is taken from" >&2
    exit 2
fi

for schema in "$CHAT_SCHEMA" "$HISTORY_SCHEMA" "$CHECKPOINTS_SCHEMA" "$KB_SCHEMA" \
    "$DESCRIBER_SCHEMA" "$IX_SCHEMA" "$STUDIO_SCHEMA" "$CATALOG_SCHEMA"; do
    if [[ ! $schema =~ ^[a-z_][a-z0-9_]*$ ]]; then
        echo "schema name '$schema' must be a lower-case identifier (letters, digits, _)" >&2
        exit 2
    fi
done

mkdir -p "$out"
if [ -n "$(ls -A "$out")" ]; then
    echo "directory $out is not empty: the copy goes into an empty directory" >&2
    exit 2
fi

export PGOPTIONS="${PGOPTIONS:-} -c default_transaction_read_only=on"

: > "$out/extensions.txt"
: > "$out/functions.txt"

tables=(
    "$CHAT_SCHEMA.users"
    "$CHAT_SCHEMA.threads"
    "$CHAT_SCHEMA.feedbacks"
    "$CHAT_SCHEMA.elements"
    "$CHAT_SCHEMA.agent_user_settings"
    "$CHAT_SCHEMA.agent_chat_settings"
    "$CHAT_SCHEMA.connections"
    "$CHAT_SCHEMA.roles"
    "$CHAT_SCHEMA.grants"
    "$HISTORY_SCHEMA.history_records"
    "$CHECKPOINTS_SCHEMA.checkpoints"
    "$CHECKPOINTS_SCHEMA.checkpoint_blobs"
    "$CHECKPOINTS_SCHEMA.checkpoint_writes"
    "$KB_SCHEMA.kb_collections"
    "$KB_SCHEMA.kb_chunks"
    "$KB_SCHEMA.kb_sources"
    "$DESCRIBER_SCHEMA.node"
    "$DESCRIBER_SCHEMA.edge"
    "$IX_SCHEMA.surface"
    "$IX_SCHEMA.aspect"
    "$IX_SCHEMA.surface_aspect"
    "$IX_SCHEMA.index_table"
    "$IX_SCHEMA.surface_url"
    "$IX_SCHEMA.surface_prompt"
    "$IX_SCHEMA.node"
    "$IX_SCHEMA.tree"
    "$IX_SCHEMA.edge"
    "$IX_SCHEMA.cfl_space"
    "$IX_SCHEMA.cfl_page"
    "$IX_SCHEMA.cfl_blogpost"
    "$IX_SCHEMA.cfl_attachment"
    "$IX_SCHEMA.cfl_comment"
    "$IX_SCHEMA.cfl_page_link"
    "$IX_SCHEMA.pg_meta_edge"
    "$IX_SCHEMA.pg_meta_database"
    "$IX_SCHEMA.pg_meta_schema"
    "$IX_SCHEMA.pg_meta_table"
    "$IX_SCHEMA.pg_meta_column"
    "$IX_SCHEMA.pg_meta_view"
    "$IX_SCHEMA.pg_meta_index"
    "$IX_SCHEMA.pg_meta_sequence"
    "$IX_SCHEMA.pg_meta_routine"
    "$IX_SCHEMA.pg_meta_constraint"
    "$IX_SCHEMA.pg_meta_trigger"
    "$IX_SCHEMA.pg_meta_type"
    "$IX_SCHEMA.pg_meta_statistics"
    "$IX_SCHEMA.ch_meta_edge"
    "$IX_SCHEMA.ch_meta_server"
    "$IX_SCHEMA.ch_meta_database"
    "$IX_SCHEMA.ch_meta_table"
    "$IX_SCHEMA.ch_meta_view"
    "$IX_SCHEMA.ch_meta_column"
    "$IX_SCHEMA.ch_meta_index"
    "$IX_SCHEMA.ch_meta_projection"
    "$IX_SCHEMA.ch_meta_dictionary"
    "$IX_SCHEMA.ch_meta_function"
    "$IX_SCHEMA.ora_meta_edge"
    "$IX_SCHEMA.ora_meta_database"
    "$IX_SCHEMA.ora_meta_schema"
    "$IX_SCHEMA.ora_meta_table"
    "$IX_SCHEMA.ora_meta_view"
    "$IX_SCHEMA.ora_meta_mview"
    "$IX_SCHEMA.ora_meta_column"
    "$IX_SCHEMA.ora_meta_constraint"
    "$IX_SCHEMA.ora_meta_index"
    "$IX_SCHEMA.ora_meta_sequence"
    "$IX_SCHEMA.ora_meta_synonym"
    "$IX_SCHEMA.ora_meta_trigger"
    "$IX_SCHEMA.ora_meta_routine"
    "$IX_SCHEMA.ix_fts"
    "$IX_SCHEMA.ix_trgm"
    "$IX_SCHEMA.ix_emb_e5_1024"
    "$IX_SCHEMA.llm_description"
    "$STUDIO_SCHEMA.users"
    "$STUDIO_SCHEMA.connections"
    "$STUDIO_SCHEMA.roles"
    "$STUDIO_SCHEMA.grants"
    "$STUDIO_SCHEMA.connection_syncs"
    "$STUDIO_SCHEMA.connection_versions"
    "$STUDIO_SCHEMA.processes"
    "$STUDIO_SCHEMA.groups"
    "$STUDIO_SCHEMA.nodes"
    "$STUDIO_SCHEMA.flows"
    "$STUDIO_SCHEMA.process_versions"
    "$STUDIO_SCHEMA.drafts"
    "$STUDIO_SCHEMA.draft_ops"
    "$STUDIO_SCHEMA.shares"
    "$STUDIO_SCHEMA.process_upgrade_runs"
    "$STUDIO_SCHEMA.process_upgrades"
    "$CATALOG_SCHEMA.pg_databases"
    "$CATALOG_SCHEMA.pg_schemas"
    "$CATALOG_SCHEMA.pg_relations"
    "$CATALOG_SCHEMA.pg_columns"
    "$CATALOG_SCHEMA.pg_constraints"
    "$CATALOG_SCHEMA.pg_indexes"
    "$CATALOG_SCHEMA.pg_sequences"
    "$CATALOG_SCHEMA.pg_routines"
    "$CATALOG_SCHEMA.pg_routine_args"
    "$CATALOG_SCHEMA.pg_types"
    "$CATALOG_SCHEMA.ch_databases"
    "$CATALOG_SCHEMA.ch_tables"
    "$CATALOG_SCHEMA.ch_columns"
    "$CATALOG_SCHEMA.ch_dictionaries"
    "$CATALOG_SCHEMA.ch_dictionary_attributes"
    "$CATALOG_SCHEMA.link"
)

sequences=(
    "$DESCRIBER_SCHEMA.node_id_seq"
    "$DESCRIBER_SCHEMA.edge_id_seq"
    "$IX_SCHEMA.node_id_seq"
    "$IX_SCHEMA.tree_id_seq"
    "$IX_SCHEMA.edge_id_seq"
    "$CATALOG_SCHEMA.pg_databases_id_seq"
    "$CATALOG_SCHEMA.pg_schemas_id_seq"
    "$CATALOG_SCHEMA.pg_relations_id_seq"
    "$CATALOG_SCHEMA.pg_columns_id_seq"
    "$CATALOG_SCHEMA.pg_constraints_id_seq"
    "$CATALOG_SCHEMA.pg_indexes_id_seq"
    "$CATALOG_SCHEMA.pg_sequences_id_seq"
    "$CATALOG_SCHEMA.pg_routines_id_seq"
    "$CATALOG_SCHEMA.pg_routine_args_id_seq"
    "$CATALOG_SCHEMA.pg_types_id_seq"
    "$CATALOG_SCHEMA.ch_databases_id_seq"
    "$CATALOG_SCHEMA.ch_tables_id_seq"
    "$CATALOG_SCHEMA.ch_columns_id_seq"
    "$CATALOG_SCHEMA.ch_dictionaries_id_seq"
    "$CATALOG_SCHEMA.ch_dictionary_attributes_id_seq"
    "$CATALOG_SCHEMA.link_id_seq"
)

types=(
    "$IX_SCHEMA.surface_e"
    "$IX_SCHEMA.aspect_class_e"
    "$IX_SCHEMA.aspect_e"
    "$IX_SCHEMA.index_kind_e"
    "$IX_SCHEMA.pg_meta_edge_role_e"
    "$IX_SCHEMA.ch_meta_edge_role_e"
    "$IX_SCHEMA.ora_meta_edge_role_e"
)

functions=(
    "$KB_SCHEMA.immutable_unaccent"
)

extensions=(vector pg_trgm btree_gin unaccent)

query() {
    psql -X -q -At -v ON_ERROR_STOP=1 -c "$1"
}

# Объект есть в базе — строка «схема<TAB>имя»; таблица и последовательность
# ищутся по pg_class, тип — по pg_type, функция — по pg_proc.
present() {
    local kind=$1
    local name=$2
    local schema=${name%%.*}
    local object=${name#*.}
    case $kind in
        relation)
            query "select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = '$schema' and c.relname = '$object'"
            ;;
        type)
            query "select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace where n.nspname = '$schema' and t.typname = '$object'"
            ;;
        function)
            query "select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = '$schema' and p.proname = '$object' limit 1"
            ;;
    esac
}

dumped_tables=()
dumped_sequences=()
dumped_types=()
dumped_functions=()
schemas=()

remember_schema() {
    local schema=$1
    local known
    for known in "${schemas[@]}"; do
        if [ "$known" = "$schema" ]; then
            return
        fi
    done
    schemas+=("$schema")
}

for name in "${tables[@]}"; do
    if [ -z "$(present relation "$name")" ]; then
        echo "skipped table $name: it is not in $PGDATABASE (the component that uses it is not deployed)"
        continue
    fi
    dumped_tables+=("$name")
    remember_schema "${name%%.*}"
done

for name in "${sequences[@]}"; do
    if [ -z "$(present relation "$name")" ]; then
        echo "skipped sequence $name: it is not in $PGDATABASE"
        continue
    fi
    dumped_sequences+=("$name")
done

for name in "${types[@]}"; do
    if [ -z "$(present type "$name")" ]; then
        echo "skipped type $name: it is not in $PGDATABASE"
        continue
    fi
    dumped_types+=("$name")
    remember_schema "${name%%.*}"
done

for name in "${functions[@]}"; do
    if [ -z "$(present function "$name")" ]; then
        echo "skipped function $name: it is not in $PGDATABASE"
        continue
    fi
    dumped_functions+=("$name")
    remember_schema "${name%%.*}"
done

if [ ${#dumped_tables[@]} -eq 0 ]; then
    echo "none of the application tables is in $PGDATABASE: check PGDATABASE and the schema variables" >&2
    exit 1
fi

for name in "${extensions[@]}"; do
    query "select e.extname || ' ' || n.nspname from pg_extension e join pg_namespace n on n.oid = e.extnamespace where e.extname = '$name'" >> "$out/extensions.txt"
done

# Схемы, типы и функции берутся из схемного дампа их схем: из его оглавления
# в prelude.sql попадают только названные объекты.
schema_args=()
for schema in "${schemas[@]}"; do
    schema_args+=(-n "\"$schema\"")
done

objects="$out/objects.tmp"
pg_dump -Fc --schema-only --no-owner --no-privileges "${schema_args[@]}" -f "$objects"

wanted="$out/prelude.list"
: > "$wanted"
toc=$(pg_restore -l "$objects")
for schema in "${schemas[@]}"; do
    grep -E "^[0-9]+; [0-9]+ [0-9]+ SCHEMA - $schema( |$)" <<< "$toc" >> "$wanted"
done
for name in "${dumped_types[@]}"; do
    grep -E "^[0-9]+; [0-9]+ [0-9]+ TYPE ${name%%.*} ${name#*.}( |$)" <<< "$toc" >> "$wanted"
done
for name in "${dumped_functions[@]}"; do
    grep -E "^[0-9]+; [0-9]+ [0-9]+ FUNCTION ${name%%.*} ${name#*.}\(" <<< "$toc" >> "$wanted"
    query "select p.oid::regprocedure from pg_proc p join pg_namespace n on n.oid = p.pronamespace where n.nspname = '${name%%.*}' and p.proname = '${name#*.}' and p.proconfig is null" >> "$out/functions.txt"
done

pg_restore --no-owner --no-privileges -L "$wanted" -f "$out/prelude.sql" "$objects"
rm "$objects" "$wanted"

table_args=()
for name in "${dumped_tables[@]}" "${dumped_sequences[@]}"; do
    table_args+=(-t "\"${name%%.*}\".\"${name#*.}\"")
done

pg_dump -Fc --no-owner --no-privileges "${table_args[@]}" -f "$out/tables.dump"

for name in "${dumped_tables[@]}"; do
    rows=$(query "select count(*) from \"${name%%.*}\".\"${name#*.}\"")
    printf '%s\t%s\n' "$name" "$rows" >> "$out/counts.tsv"
done

echo "copied ${#dumped_tables[@]} tables, ${#dumped_sequences[@]} sequences, ${#dumped_types[@]} types and ${#dumped_functions[@]} functions of $PGDATABASE into $out"
