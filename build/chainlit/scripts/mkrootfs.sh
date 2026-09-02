#!/bin/sh
# Образ корня песочницы: дерево $1 -> ext4-файл $2 тем же mke2fs, что делает шаблон workspace.
# Размер — занятое место с запасом в 5% и reserve_mb; образ монтируется read-only.
set -eu

tree=$1
out=$2
python_version=$3
reserve_mb=${4:-512}
e2fs_src=/tmp/e2fs-src

PYTHONHOME="$tree/usr/local" "$tree/usr/local/bin/python3" -m compileall -q -j 0 \
    -s "$tree" -p / -x '/(test|tests|lib2to3|idle_test)/' \
    "$tree/usr/local/lib/python$python_version" "$tree/usr/src"

used_mb=$(du -sm "$tree" | cut -f1)
size_mb=$(( used_mb + used_mb / 20 + reserve_mb ))
inodes=$(find "$tree" | wc -l)
inodes=$(( inodes + inodes / 5 + 1000 ))

truncate -s "${size_mb}M" "$out"
cd "$e2fs_src"
MKE2FS_CONFIG="$e2fs_src/misc/mke2fs.conf" ./misc/mke2fs -F -q -t ext4 -O ^has_journal -m 0 \
    -L rootfs -N "$inodes" -d "$tree" "$out"
ls -lh "$out"
