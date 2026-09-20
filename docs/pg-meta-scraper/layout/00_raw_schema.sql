-- Сырые таблицы каталога: по одной на @name scrape-запроса, колонки в порядке выборки.
-- Все таблицы временные: живут в сессии загрузчика и исчезают с ней. Python делает
-- COPY raw_<name> FROM STDIN с заголовком, ничего не преобразуя.
-- raw_source заполняется одной строкой: откуда снято, для адресов node.
create temp table raw_source (
    scheme    text not null,
    host      text not null,
    port      int  not null,
    database  text not null
);

create temp table raw_database (oid oid, datname text, datdba oid, owner_name text, encoding int, encoding_name text, datcollate text, datctype text, row_xmin text);
create temp table raw_namespace (oid oid, nspname text, nspowner oid, owner_name text, row_xmin text);
create temp table raw_language (oid oid, lanname text, row_xmin text);
create temp table raw_am (oid oid, amname text, row_xmin text);
create temp table raw_opclass (oid oid, opcname text, opcmethod oid, opcintype oid, row_xmin text);
create temp table raw_foreign_server (oid oid, srvname text, srvfdw oid, srvoptions text[], row_xmin text);
create temp table raw_tablespace (oid oid, spcname text, row_xmin text);
create temp table raw_shdescription (objoid oid, classoid oid, description text, row_xmin text);

create temp table raw_class (oid oid, relname text, relnamespace oid, relkind text, relowner oid, owner_name text, relam oid, reltype oid, reloftype oid,
    relnatts int, relchecks int, relhasindex bool, relhastriggers bool, relhassubclass bool, relispartition bool, relstorage text,
    reltablespace oid, relpersistence text, relpartbound text, reltuples float8, relpages int, row_xmin text);
create temp table raw_type (oid oid, typname text, typnamespace oid, typowner oid, typtype text, typcategory text, typrelid oid, typbasetype oid, typelem oid, typnotnull bool, base_type text, row_xmin text);
create temp table raw_proc (oid oid, proname text, pronamespace oid, proowner oid, owner_name text, prolang oid, prokind text, prorettype oid, proretset bool,
    provolatile text, prosecdef bool, proargtypes oid[], proargnames text[], proargmodes text[], identity_args text, result_type text, row_xmin text);

create temp table raw_attribute (attrelid oid, attnum int, attname text, atttypid oid, atttypmod int, data_type text, attnotnull bool, atthasdef bool,
    attidentity text, attgenerated text, row_xmin text);
create temp table raw_attrdef (oid oid, adrelid oid, adnum int, expr text, row_xmin text);
create temp table raw_index (indexrelid oid, indrelid oid, indnatts int, indnkeyatts int, indisunique bool, indisprimary bool, indisexclusion bool,
    indimmediate bool, indisvalid bool, indkey int2[], indoption int2[], indclass oid[], indexprs text, indpred text, row_xmin text);
create temp table raw_constraint (oid oid, conname text, connamespace oid, contype text, conrelid oid, contypid oid, conindid oid, confrelid oid,
    condeferrable bool, condeferred bool, convalidated bool, conparentid oid, conislocal bool, coninhcount int,
    confupdtype text, confdeltype text, confmatchtype text, conkey int2[], confkey int2[], conpfeqop oid[], conexclop oid[],
    confdelsetcols int2[], definition text, row_xmin text);
create temp table raw_trigger (oid oid, tgrelid oid, tgname text, tgfoid oid, tgtype int, tgenabled text, tgconstraint oid, tgconstrrelid oid, tgparentid oid, tgattr int2[], row_xmin text);
create temp table raw_inherits (inhrelid oid, inhparent oid, inhseqno int, row_xmin text);
create temp table raw_partitioned_table (partrelid oid, partstrat text, partnatts int, partattrs int2[], partclass oid[], partdefid oid, row_xmin text);
create temp table raw_rewrite (oid oid, ev_class oid, rulename text, ev_type text, row_xmin text);
create temp table raw_sequence (seqrelid oid, seqtypid oid, data_type text, seqstart bigint, seqincrement bigint, seqmin bigint, seqmax bigint, seqcache bigint, seqcycle bool, row_xmin text);
create temp table raw_statistic_ext (oid oid, stxname text, stxnamespace oid, stxrelid oid, stxkeys int2[], stxkind text[], row_xmin text);
create temp table raw_enum (oid oid, enumtypid oid, enumlabel text, enumsortorder float8, row_xmin text);
create temp table raw_range (rngtypid oid, rngsubtype oid, subtype_name text, row_xmin text);
create temp table raw_foreign_table (ftrelid oid, ftserver oid, ftoptions text[], row_xmin text);
create temp table raw_description (objoid oid, classoid oid, objsubid int, description text, row_xmin text);
create temp table raw_depend (classid oid, objid oid, objsubid int, refclassid oid, refobjid oid, refobjsubid int, deptype text, row_xmin text);

create temp table raw_gp_distribution_policy (localoid oid, policytype text, numsegments int, distkey int2[], row_xmin text);
create temp table raw_gp_exttable (reloid oid, urilocation text[], fmttype text, row_xmin text);
create temp table raw_gp_appendonly (relid oid, columnstore bool, row_xmin text);
create temp table raw_gp_partition (oid oid, parrelid oid, parkind text, parlevel int, paratts int2[], row_xmin text);
create temp table raw_gp_partition_rule (oid oid, paroid oid, parchildrelid oid, parparentrule oid, parname text, parruleord int, row_xmin text);
