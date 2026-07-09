#!/bin/sh
# Единая PEP 440-версия всех boba-пакетов из git-тега. СТРОГО.
#
# Допустим ТОЛЬКО точный тег на ЧИСТОМ дереве, формата:
#   release-X.Y.Z  ->  X.Y.Z
#   dev-X.Y.Z      ->  X.Y.Z.dev0
# где X, Y, Z — целые числа.
#
# Любое отклонение — грязное дерево, коммиты поверх тега, чужой формат тега,
# отсутствие тега — печатает причину в stderr и завершается с кодом 1
# (сборка wheel'а падает).
#
# Используется Makefile (build/wheels) как источник SETUPTOOLS_SCM_PRETEND_VERSION.
# Аргумент: путь к репозиторию (по умолчанию — текущая директория).
set -eu

repo="${1:-.}"

die() { echo "scmversion: $*" >&2; exit 1; }

git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 \
    || die "не git-репозиторий: $repo"

# 1) дерево должно быть чистым
if [ -n "$(git -C "$repo" status --porcelain 2>/dev/null)" ]; then
    die "рабочее дерево грязное — закоммить/спрячь изменения перед сборкой wheel"
fi

# 2) HEAD должен быть ТОЧНО на теге
tag="$(git -C "$repo" describe --tags --exact-match 2>/dev/null)" \
    || die "HEAD не на теге — поставь тег release-X.Y.Z или dev-X.Y.Z на этот коммит"

# 3) строгий разбор формата: <kind>-<X.Y.Z>
kind="${tag%%-*}"        # часть до первого '-'
ver="${tag#*-}"          # часть после первого '-'

case "$kind" in
    release|dev) : ;;
    *) die "недопустимый тег '$tag': разрешены только release-X.Y.Z и dev-X.Y.Z" ;;
esac

# ровно три целочисленных компонента, ничего лишнего
printf '%s' "$ver" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
    || die "недопустимый тег '$tag': версия должна быть X.Y.Z (три целых числа)"

# 4) отображение в PEP 440
if [ "$kind" = dev ]; then
    printf '%s.dev0\n' "$ver"
else
    printf '%s\n' "$ver"
fi
