#!/bin/sh
# Системные библиотеки образа, от которых зависят интерпретатор и колёса дерева $1,
# кроме glibc: пишет в stdout tar с файлами под их soname (настоящие файлы, без ссылок).
# Запускается внутри образа приложения; нужен дереву релиза на системе, где этих
# библиотек нет (не та, на которой собран образ).
set -eu
root=$1
bundle=$(mktemp -d)
dpkg -L libc6 | sort -u > "$bundle.glibc"
find "$root/third" "$root/app" -type f \( -name '*.so' -o -name '*.so.*' -o -perm -u+x \) |
    while read -r file; do
        ldd "$file" 2> /dev/null || true
    done | awk '/=> \// {print $3}' | sort -u |
    while read -r lib; do
        real=$(readlink -f "$lib")
        case "$lib" in
            "$root"/*) continue ;;
        esac
        if grep -qx "$real" "$bundle.glibc" || grep -qx "$lib" "$bundle.glibc"; then
            continue
        fi
        name=$(basename "$lib")
        if [ -e "$root/third/lib/$name" ]; then
            continue
        fi
        cp "$real" "$bundle/$name"
    done
tar -C "$bundle" -cf - .
