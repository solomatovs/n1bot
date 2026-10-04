#!/bin/sh
# Шаблон workspace песочницы: пустой ext4-файл $1 размером $2 через mke2fs
# из src/sandbox/tools; итог отдаётся владельцу хоста $3.
set -eu

out=$1
size=$2
owner=$3

rm -f "$out"
truncate -s "$size" "$out"
mke2fs -F -q -t ext4 -O ^has_journal -m 0 -L workspace "$out"
chown "$owner" "$out"
ls -lh "$out"
