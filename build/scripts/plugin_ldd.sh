#!/bin/sh
# Проверка программы плагина в голом корне его образа (стадия plugin-bin-root):
# каждая ELF в /plugin находит все библиотеки, а снаружи /plugin берёт только
# glibc (пакет libc6) и пакеты, которые поставил ключ apt плагина. Библиотека
# пакета базового образа glibc, кроме libc6, — отказ: на неё программа
# опирается случайно. Затем самопроверка программы по описи сборки.
# $1 — id плагина, $2 — файл со списком пакетов базового образа glibc.
set -eu

plugin=$1
base=$2
report=$(mktemp -d)

find /plugin -type f \( -name '*.so' -o -name '*.so.*' -o -perm -u+x \) | sort > "$report/files"
while read -r file; do
    magic=$(od -An -tx1 -N4 "$file" | tr -d ' ')
    if [ "$magic" != 7f454c46 ]; then
        continue
    fi

    # у статической ELF ldd отвечает «not a dynamic executable» с кодом 1
    ldd "$file" > "$report/ldd" 2>&1 || true
    sed -n "s|^\s*\(\S*\) => not found.*|$file \1|p" "$report/ldd" >> "$report/missing"
    awk -v elf="$file" '{for (i = 1; i < NF; i++) if ($i == "=>" && $(i + 1) ~ /^\//) print $(i + 1), elf}' "$report/ldd" >> "$report/resolved"
done < "$report/files"

if [ -s "$report/missing" ]; then
    echo "plugin $plugin: libraries not found in the bare root (ELF, soname):" >&2
    sort -u "$report/missing" >&2
    echo "add the system package that ships them to the apt key of the plugin declaration" >&2
    exit 1
fi

touch "$report/resolved"
cut -d' ' -f1 "$report/resolved" | sort -u | while read -r lib; do
    case "$lib" in
        /plugin/*) continue ;;
    esac

    real=$(readlink -f "$lib")
    owner=$(dpkg -S "$real" 2> /dev/null | cut -d: -f1 | head -n1)
    if [ -z "$owner" ]; then
        owner=$(dpkg -S "$lib" 2> /dev/null | cut -d: -f1 | head -n1)
    fi

    echo "$lib ${owner:-?}"
done > "$report/outside"

echo "plugin $plugin: libraries outside /plugin (path, package):"
cat "$report/outside"

awk '$2 != "libc6"' "$report/outside" | while read -r lib owner; do
    if grep -qx "$owner" "$base"; then
        echo "$lib $owner"
    fi
done > "$report/accidental"

if [ -s "$report/accidental" ]; then
    echo "plugin $plugin: the program loads libraries of the base image packages other than glibc:" >&2
    cat "$report/accidental" >&2
    echo "loaded by:" >&2
    cut -d' ' -f1 "$report/accidental" | while read -r lib; do
        grep "^$lib " "$report/resolved" >&2
    done
    echo "the build must bundle them into /plugin/run.dist or the plugin declares them by apt" >&2
    exit 1
fi

# окружение песочницы (SandboxDefaults): образ docker несёт свои LC_*, песочница — нет
env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp LANG=C.UTF-8 /plugin/run self-check
