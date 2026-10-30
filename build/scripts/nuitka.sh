#!/bin/sh
# Компиляция программы boba или программы плагина Nuitka: docker run образа дерева колёс
# (app-site программы или plugin-site плагина, оба поверх стадии nuitka-tools) без сети,
# build/conf на /conf (nuitka.yml), кэш ccache и Nuitka — каталог хоста NUITKA_CACHE на
# /cache. Результат — каталог <имя>.dist в $4. Опись boba.core.package_data (данные
# пакетов и entry points групп boba.*) даёт флаги Nuitka и едет в бинарник; самопроверка
# по ней в конце роняет сборку, если чего-то внутри нет. Системные библиотеки, которые
# Nuitka считает общими (libz), кроме glibc, едут в каталог программы из образа сборки:
# корень плагина голый.
# Аргументы: $1 — пути python дерева через «:» (опись — по первому), $2 — файл входа,
# $3 — имя программы, $4 — каталог результата, $5 — число заданий; $6 — id плагина: у
# программы плагина рядом с каталогом ложится run, вход с id для /plugin корня образа
set -eu

site=$1
entry=$2
name=$3
out=$4
jobs=$5
plugin=${6:-}
work=/tmp/nuitka
survey=${site%%:*}

export PYTHONPATH="$site:/opt/bin-tools" PATH="/opt/bin-tools/bin:$PATH"
export CCACHE_DIR=/cache/ccache NUITKA_CACHE_DIR=/cache/nuitka HOME=/tmp
mkdir -p "$CCACHE_DIR" "$NUITKA_CACHE_DIR" "$work"
ccache --max-size=20G > /dev/null
ccache --zero-stats > /dev/null

python3 -m boba.core.package_data "$survey" "$work/package_data.json" boba. > "$work/flags"
# gssapi грузит свои расширения (gssapi.raw.ext_*) импортом по имени: пакет — целиком,
# если он есть в дереве
if python3 -c 'import importlib.util, sys; sys.exit(importlib.util.find_spec("gssapi") is None)'; then
    printf '%s\n' --include-package=gssapi --nofollow-import-to=gssapi.tests >> "$work/flags"
fi

start=$(date +%s)
# shellcheck disable=SC2046 # флаги описи — по одному на строку, без пробелов
python3 -m nuitka --standalone --static-libpython=yes --output-filename="$name.bin" \
    --update-check=never --output-dir="$work/out" --jobs="$jobs" \
    --user-package-configuration-file=/conf/nuitka.yml \
    $(cat "$work/flags") "$entry"
echo "nuitka: $name compiled in $(( $(date +%s) - start )) s"
ccache --show-stats | grep -E 'cache hit|cache miss|cache size'

dist="$work/out/$(basename "$entry" .py).dist"
dpkg -L libc6 | sort -u > "$work/glibc"
find "$dist" -type f \( -name '*.so' -o -name '*.so.*' -o -perm -u+x \) |
    while read -r file; do
        ldd "$file" 2> /dev/null || true
    done | awk '/=> \// {print $3}' | sort -u |
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

if [ -n "$plugin" ]; then
    "$dist/$name.bin" "$plugin" self-check
    printf '#!/bin/sh\nexec /plugin/%s.dist/%s.bin %s "$@"\n' "$name" "$name" "$plugin" > "$out/run"
    chmod 0755 "$out/run"
else
    "$dist/$name.bin" self-check
    # справка службы импортирует её целиком: недостающий в бинарнике модуль роняет сборку
    "$dist/$name.bin" serve --help > /dev/null
fi

mv "$dist" "$out/$name.dist"
du -sh "$out/$name.dist"
