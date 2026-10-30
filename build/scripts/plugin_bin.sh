#!/bin/sh
# Программа плагина каталогом Nuitka: компиляция в контейнере образа стадии
# plugin-bin-tools (site-packages плагина, Nuitka, ccache), том кэша — /cache.
# $1 — id плагина, $2 — каталог результата: в нём появится plugin/ с программой
# run и её каталогом run.dist, как они лягут в корень образа. $3 — число заданий,
# $4 — владелец результата на хосте.
# Опись данных пакетов и entry points групп boba.* (boba.core.package_data) даёт
# флаги Nuitka и едет в бинарник; self-check по ней make зовёт уже в голом корне.
set -eu

plugin=$1
out=$2
jobs=$3
owner=$4
work=/tmp/plugin-bin

export CCACHE_DIR=/cache/ccache
export NUITKA_CACHE_DIR=/cache/nuitka
mkdir -p "$CCACHE_DIR" "$NUITKA_CACHE_DIR" "$work"

python3 -m boba.core.package_data /opt/code-sandbox "$work/package_data.json" boba. > "$work/flags"
cat "$work/flags"

# shellcheck disable=SC2046 # флаги описи — по слову на флаг
python3 -m nuitka --standalone --static-libpython=yes --output-filename=run.bin --update-check=never \
    --output-dir="$work/nuitka" --jobs="$jobs" \
    $(cat "$work/flags") \
    /opt/code-sandbox/boba/sandbox/program.py

mkdir -p "$out/plugin"
dist="$out/plugin/run.dist"
mv "$work/nuitka/program.dist" "$dist"

# системные библиотеки, которые Nuitka считает общими (libz), кроме glibc, едут в
# каталог программы из образа сборки; чего нет и здесь (libGL), даёт ключ apt плагина
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
        name=$(basename "$lib")
        if [ -e "$dist/$name" ]; then
            continue
        fi
        echo "bundled from the build image: $lib"
        cp "$real" "$dist/$name"
    done

printf '#!/bin/sh\nexec /plugin/run.dist/run.bin %s "$@"\n' "$plugin" > "$out/plugin/run"
chmod 0755 "$out/plugin/run"

ccache -s
chown -R "$owner" "$out/plugin"
du -sh "$out/plugin/run.dist"
