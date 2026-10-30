#!/bin/sh
# Компиляция программы boba Nuitka в контейнере образа program-tools (make build): дерево
# колёс приложения — /opt/app-site, кэш ccache и Nuitka — том /cache, результат — каталог
# <программа>.dist в $3. Опись boba.runtime.program даёт флаги данных пакетов и entry points
# и едет в бинарник; самопроверка по ней в конце роняет сборку, если чего-то внутри нет.
# Аргументы: $1 — файл входа относительно /opt/app-site, $2 — имя программы, $3 — каталог
# результата
set -eu

entry=$1
name=$2
out=$3
work=/tmp/nuitka

export CCACHE_DIR=/cache/ccache NUITKA_CACHE_DIR=/cache/nuitka HOME=/tmp
mkdir -p "$CCACHE_DIR" "$NUITKA_CACHE_DIR"
ccache --max-size=20G > /dev/null
ccache --zero-stats > /dev/null

python3 -m boba.runtime.program /opt/app-site /tmp/program.json > /tmp/program.flags

start=$(date +%s)
# shellcheck disable=SC2046 # флаги описи — по одному на строку, без пробелов
python3 -m nuitka --standalone --static-libpython=yes --output-filename="$name.bin" \
    --update-check=never --output-dir="$work" --jobs="$JOBS" \
    --user-package-configuration-file=/opt/nuitka/nuitka.yml \
    --include-package=gssapi --nofollow-import-to=gssapi.tests \
    $(cat /tmp/program.flags) "/opt/app-site/$entry"
echo "nuitka: $name compiled in $(( $(date +%s) - start )) s"
ccache --show-stats | grep -E 'cache hit|cache miss|cache size' || true

dist="$work/$(basename "$entry" .py).dist"
"$dist/$name.bin" self-check
# справка службы импортирует её целиком: недостающий в бинарнике модуль роняет сборку
"$dist/$name.bin" serve --help > /dev/null
mv "$dist" "$out/$name.dist"
