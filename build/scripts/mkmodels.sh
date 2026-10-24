#!/bin/sh
# Образ данных песочницы: каталог весов модели $2 (смонтирован read-only) ->
# ext4-файл $1 через mke2fs из runtime/sandbox/tools; метка $3, запас reserve_mb $4,
# итог отдаётся владельцу хоста $5. Секции монтируют образ fuse2fs read-only, поэтому
# журнал и резерв root не нужны. Размер — занятое место с запасом в 5% и reserve_mb.
# $1 — файл во временном каталоге: на место его ставит Makefile переименованием.
set -eu

out=$1
src=$2
label=$3
reserve_mb=$4
owner=$5

used_mb=$(du -sm "$src" | cut -f1)
size_mb=$(( used_mb + used_mb / 20 + reserve_mb ))
inodes=$(find "$src" | wc -l)
inodes=$(( inodes + inodes / 5 + 1000 ))

rm -f "$out"
truncate -s "${size_mb}M" "$out"
mke2fs -F -q -t ext4 -O ^has_journal -m 0 -L "$label" -N "$inodes" -d "$src" "$out"
chown "$owner" "$out"
ls -lh "$out"
