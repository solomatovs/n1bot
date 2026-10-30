#!/bin/sh
# Шаг compile: программа Nuitka из дерева пакетов. Идёт в docker run образа стадии
# site (base.Dockerfile) без сети; build/conf — на /conf, кэш ccache и Nuitka —
# каталог хоста build/cache на /cache, результат — каталог /out/$4. Опись
# boba.core.package_data (данные пакетов и entry points групп boba.*) даёт флаги
# Nuitka и едет в бинарник: по ней программа делает self-check. Системные
# библиотеки, кроме glibc, едут в каталог программы копиями из образа сборки, и
# RPATH каждой библиотеки ведёт к найденным копиям: корень песочницы и образ
# программы голые. Проверки собранного делает make: self-check в образе программы,
# build/plugin_check.sh — в корне плагина.
# Аргументы: $1 — дерево пакетов, $2 — файл входа, $3 — имя исполняемого файла,
# $4 — имя каталога результата, $5 — число заданий компилятора; дальше — флаги
# Nuitka программы
set -eu

site=$1
entry=$2
name=$3
dest=$4
jobs=$5
shift 5
work=/tmp/nuitka

export PYTHONPATH="$site:/opt/nuitka" PATH="/opt/nuitka/bin:$PATH"
export CCACHE_DIR=/cache/ccache NUITKA_CACHE_DIR=/cache/nuitka HOME=/tmp
mkdir -p "$CCACHE_DIR" "$NUITKA_CACHE_DIR" "$work"
ccache --max-size=20G > /dev/null
ccache --zero-stats > /dev/null

python3 -m boba.core.package_data "$site" "$work/package_data.json" boba. > "$work/flags"
# gssapi грузит свои расширения (gssapi.raw.ext_*) импортом по имени: пакет — целиком
if python3 -c 'import importlib.util, sys; sys.exit(importlib.util.find_spec("gssapi") is None)'; then
    printf '%s\n' --include-package=gssapi --nofollow-import-to=gssapi.tests >> "$work/flags"
fi
# платформа X11 Qt из колеса opencv-python тянет libSM и libICE, а экрана в песочнице нет
printf '%s\n' '--noinclude-dlls=cv2/qt/plugins/platforms/libqxcb.so' '--noinclude-dlls=libQt5XcbQpa*' \
    >> "$work/flags"

start=$(date +%s)
# shellcheck disable=SC2046 # флаги описи — по одному на строку, без пробелов
python3 -m nuitka --standalone --static-libpython=yes --output-filename="$name" \
    --update-check=never --output-dir="$work/out" --jobs="$jobs" \
    --user-package-configuration-file=/conf/nuitka/common.yml \
    $(cat "$work/flags") "$@" "$entry"
echo "nuitka: $dest compiled in $(( $(date +%s) - start )) s"
ccache --show-stats | grep -E 'cache hit|cache miss|cache size'

dist="$work/out/$(basename "$entry" .py).dist"
# путь к библиотеке после «=>» в выводе ldd; у статической ELF ldd отвечает кодом 1
resolved='{for (i = 1; i < NF; i++) if ($i == "=>" && $(i + 1) ~ /^\//) print $(i + 1)}'
dpkg -L libc6 | sort -u > "$work/glibc"
find "$dist" -type f \( -name '*.so' -o -name '*.so.*' -o -perm -u+x \) |
    while read -r file; do
        ldd "$file" 2> /dev/null || true
    done | awk "$resolved" | sort -u |
    while read -r lib; do
        case "$lib" in
            "$dist"/*) continue ;;
        esac
        real=$(readlink -f "$lib")
        if grep -qx "$real" "$work/glibc" || grep -qx "$lib" "$work/glibc"; then
            continue
        fi
        if [ -e "$dist/$(basename "$lib")" ]; then
            continue
        fi
        echo "bundled from the build image: $lib"
        cp "$real" "$dist/$(basename "$lib")"
    done

# библиотека, которой Nuitka (или копия выше) не дала путь к нужной копии в каталоге
# программы, брала бы зависимость из системы или не нашла бы её вовсе (numpy.libs Nuitka
# переносит в корень): RPATH дописывается каталогом найденной копии
find "$dist" -type f \( -name '*.so' -o -name '*.so.*' \) | sort > "$work/libraries"
while read -r file; do
    dir=$(dirname "$file")
    ldd "$file" 2> /dev/null |
        awk -v dist="$dist/" '$2 == "=>" && index($3, dist) != 1 {print $1}' > "$work/needed"
    add=
    while read -r soname; do
        copy=$(grep -F "/$soname" "$work/libraries" | grep -m1 "/$soname\$" || true)
        if [ -z "$copy" ]; then
            continue
        fi
        add="$add:\$ORIGIN/$(realpath --relative-to="$dir" "$(dirname "$copy")")"
    done < "$work/needed"
    if [ -z "$add" ]; then
        continue
    fi
    rpath=$(patchelf --print-rpath "$file")
    if [ -z "$rpath" ]; then
        add=${add#:}
    fi
    echo "rpath for $file: $rpath$add"
    patchelf --set-rpath "$rpath$add" "$file"
done < "$work/libraries"

rm -rf "/out/$dest"
mv "$dist" "/out/$dest"
du -sh "/out/$dest"
