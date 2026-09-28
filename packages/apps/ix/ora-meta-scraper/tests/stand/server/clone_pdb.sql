-- SCRAPEPDB на Oracle 12.2 (compose/oracle) — клон ORCLPDB1 с демо-схемами образа (HR, OE,
-- PM, SH, IX, SCOTT), которые входят в эталон. Без local undo источник на время клона
-- открывается только на чтение; &1 — каталог файлов ORCLPDB1, &2 — каталог клона.
set pages 0 feedback off verify off
whenever sqlerror exit failure
alter pluggable database orclpdb1 close immediate;
alter pluggable database orclpdb1 open read only;
create pluggable database scrapepdb from orclpdb1 file_name_convert = ('&1/', '&2/');
alter pluggable database orclpdb1 close immediate;
alter pluggable database orclpdb1 open;
alter pluggable database scrapepdb open;
alter pluggable database orclpdb1 save state;
alter pluggable database scrapepdb save state;
exit
