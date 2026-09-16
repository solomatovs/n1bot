#!/bin/sh
# Образ корня песочницы: дерево tar'ами со stdin (несколько архивов подряд) ->
# ext4-файл $1 через mke2fs из src/sandbox/tools. Запускается root'ом в контейнере
# glibc, чтобы владельцы файлов из tar сохранились; итог отдаётся владельцу хоста $4.
# Байткод компилируется python'ом дерева: образ монтируется read-only, .pyc на
# лету не появятся. Размер — занятое место с запасом в 5% и reserve_mb $3.
set -eu

out=$1
python_version=$2
reserve_mb=$3
owner=$4
tree=/tree

mkdir -p "$tree"
tar -x -i -f - -C "$tree"

PYTHONHOME="$tree/usr/local" LD_LIBRARY_PATH="$tree/usr/local/lib" "$tree/usr/local/bin/python3" \
    -m compileall -q -j 0 -s "$tree" -p / -x '/(test|tests|lib2to3|idle_test)/' \
    "$tree/usr/local/lib/python$python_version" "$tree/usr/src"

used_mb=$(du -sm "$tree" | cut -f1)
size_mb=$(( used_mb + used_mb / 20 + reserve_mb ))
inodes=$(find "$tree" | wc -l)
inodes=$(( inodes + inodes / 5 + 1000 ))

rm -f "$out"
truncate -s "${size_mb}M" "$out"
mke2fs -F -q -t ext4 -O ^has_journal -m 0 -L rootfs -N "$inodes" -d "$tree" "$out"
chown "$owner" "$out"
ls -lh "$out"
