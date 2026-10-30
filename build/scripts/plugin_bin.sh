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

# gssapi грузит свои cython-модули из расширений, импорт их Nuitka не видит
if python3 -c 'import gssapi' 2> /dev/null; then
    echo '--include-package=gssapi' >> "$work/flags"
    echo '--nofollow-import-to=gssapi.tests' >> "$work/flags"
fi

# платформа X11 Qt из колеса opencv-python тянет libSM и libICE, а экрана в песочнице нет
echo '--noinclude-dlls=cv2/qt/plugins/platforms/libqxcb.so' >> "$work/flags"
echo '--noinclude-dlls=libQt5XcbQpa*' >> "$work/flags"

# shellcheck disable=SC2046 # флаги описи — по слову на флаг
python3 -m nuitka --standalone --static-libpython=yes --output-filename=run.bin --update-check=never \
    --output-dir="$work/nuitka" --jobs="$jobs" \
    $(cat "$work/flags") \
    /opt/code-sandbox/boba/sandbox/program.py

mkdir -p "$out/plugin"
dist="$out/plugin/run.dist"
mv "$work/nuitka/program.dist" "$dist"

# системные библиотеки, которые Nuitka считает общими (libz), кроме glibc, едут в
# каталог программы из образа сборки; библиотеки пакетов ключа apt плагина (libGL) в
# корень образа ставит apt, их стадия plugin-bin-tools поставила поверх списка
# build-packages.txt
dpkg -L libc6 | sort -u > "$work/glibc"
find "$dist" -type f \( -name '*.so' -o -name '*.so.*' -o -perm -u+x \) |
    while read -r file; do
        ldd "$file" 2> /dev/null || true
    done | awk '{for (i = 1; i < NF; i++) if ($i == "=>" && $(i + 1) ~ /^\//) print $(i + 1)}' | sort -u |
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
        owner=$(dpkg -S "$real" 2> /dev/null | cut -d: -f1 | head -n1)
        if [ -n "$owner" ] && ! grep -qx "$owner" /opt/bin-tools/build-packages.txt; then
            echo "left to the apt key of the plugin: $lib ($owner)"
            continue
        fi
        echo "bundled from the build image: $lib"
        cp "$real" "$dist/$name"
    done

# библиотека, которой Nuitka (или копия выше) не дала путь к нужной копии в каталоге
# программы, брала бы зависимость из системы или не нашла бы её вовсе (numpy.libs
# Nuitka переносит в корень): RPATH дописывается каталогом найденной копии
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

printf '#!/bin/sh\nexec /plugin/run.dist/run.bin %s "$@"\n' "$plugin" > "$out/plugin/run"
chmod 0755 "$out/plugin/run"

ccache -s
chown -R "$owner" "$out/plugin"
du -sh "$out/plugin/run.dist"
