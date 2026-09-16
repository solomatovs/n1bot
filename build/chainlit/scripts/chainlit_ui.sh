#!/bin/sh
# UI chainlit из исходников тега с overlay поверх (web/chainlit-ui): все пакеты
# workspace — react-client, app (фронт) и copilot (виджет), как pnpm build у upstream.
# custom_build в chainlit заменяет весь UI: и фронт, и copilot ищутся в одном каталоге.
# Зависимости pnpm ставит из реестра npm образа nodejs (в закрытом контуре — nexus).
#   chainlit_ui.sh <src.tar.gz> <overlay> <out>
# Запускается в образе nodejs (node + pnpm), как стадией Dockerfile, так и целью make.
set -eu

export HOME=/tmp HUSKY=0 CYPRESS_INSTALL_BINARY=0
PNPM_OPTS="--frozen-lockfile --config.package-manager-strict=false --filter @chainlit/react-client --filter @chainlit/app --filter @chainlit/copilot"
WORK="${UI_WORK:-/tmp/chainlit-ui}"

if [ $# -ne 3 ]; then
    echo "usage: chainlit_ui.sh <src.tar.gz> <overlay> <out>" >&2
    exit 2
fi

SRC_TARBALL="$1"
OVERLAY="$2"
OUT="$3"

unpack_sources() {
    root=$(tar -tzf "$1" | head -1 | cut -d/ -f1)
    mkdir -p "$WORK/src"
    tar -xzf "$1" -C "$WORK/src" --strip-components=1 \
        "$root/frontend" "$root/libs" "$root/package.json" "$root/pnpm-lock.yaml" \
        "$root/pnpm-workspace.yaml" "$root/.npmrc"
}

# фронт тянет шрифт и стили формул с CDN: переводим на public/vendor
patch_index() {
    sed -i \
        -e '/rel="preconnect"/d' \
        -e 's#https://fonts.googleapis.com/css2?family=Inter[^"]*#/public/vendor/inter/inter.css#' \
        -e 's#https://cdn.jsdelivr.net/npm/katex@[0-9.]*/dist/katex.min.css#/public/vendor/katex/katex.min.css#' \
        "$1"
    ! grep -E 'fonts\.(googleapis|gstatic)\.com|cdn\.jsdelivr\.net' "$1"
}

# контракт chainlit/server.py: в каталоге custom_build лежат index.html фронта и index.js copilot
check_ui() {
    for name in index.html index.js; do
        if [ ! -f "$1/$name" ]; then
            echo "chainlit_ui.sh: $1/$name is missing — the ui build is incomplete" >&2
            exit 1
        fi
    done
}

rm -rf "$WORK"
unpack_sources "$SRC_TARBALL"
cp -a "$OVERLAY/." "$WORK/src/"
cd "$WORK/src"

pnpm install $PNPM_OPTS
pnpm --filter @chainlit/react-client run build
pnpm --filter @chainlit/react-client run type-check
pnpm --filter @chainlit/app run type-check
pnpm --filter @chainlit/app run build
pnpm --filter @chainlit/copilot run build

patch_index frontend/dist/index.html
mkdir -p "$OUT"
find "$OUT" -mindepth 1 -delete
cp -a frontend/dist/. "$OUT/"
cp -a libs/copilot/dist/. "$OUT/"
check_ui "$OUT"
