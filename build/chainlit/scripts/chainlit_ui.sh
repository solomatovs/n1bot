#!/bin/sh
# Фронт chainlit из исходников тега с overlay поверх (web/chainlit-ui).
#   store <src.tar.gz> <store.tar.gz>                 — pnpm-store для offline-сборки (нужна сеть)
#   build <src.tar.gz> <store.tar.gz> <overlay> <out> — сборка dist без сети
# Запускается в образе nodejs (node + pnpm), как стадией Dockerfile, так и целью make.
set -eu

export HOME=/tmp HUSKY=0 CYPRESS_INSTALL_BINARY=0
PNPM_OPTS="--frozen-lockfile --config.package-manager-strict=false --filter @chainlit/react-client --filter @chainlit/app"
WORK="${UI_WORK:-/tmp/chainlit-ui}"

unpack_sources() {
    root=$(tar -tzf "$1" | head -1 | cut -d/ -f1)
    mkdir -p "$WORK/src"
    tar -xzf "$1" -C "$WORK/src" --strip-components=1 \
        "$root/frontend" "$root/libs" "$root/package.json" "$root/pnpm-lock.yaml" \
        "$root/pnpm-workspace.yaml" "$root/.npmrc"
}

case "${1:-}" in
store)
    rm -rf "$WORK"
    unpack_sources "$2"
    mkdir -p "$WORK/store"
    cd "$WORK/src"
    pnpm install --store-dir "$WORK/store" $PNPM_OPTS
    tar -czf "$3" -C "$WORK/store" .
    ;;
build)
    rm -rf "$WORK"
    unpack_sources "$2"
    mkdir -p "$WORK/store"
    tar -xzf "$3" -C "$WORK/store"
    cp -a "$4/." "$WORK/src/"
    cd "$WORK/src"
    pnpm install --offline --store-dir "$WORK/store" $PNPM_OPTS
    pnpm --filter @chainlit/react-client run build
    pnpm --filter @chainlit/react-client run type-check
    pnpm --filter @chainlit/app run type-check
    pnpm --filter @chainlit/app run build
    mkdir -p "$5"
    find "$5" -mindepth 1 -delete
    cp -a frontend/dist/. "$5/"
    ;;
*)
    echo "usage: chainlit_ui.sh store <src.tar.gz> <store.tar.gz> | build <src.tar.gz> <store.tar.gz> <overlay> <out>" >&2
    exit 2
    ;;
esac
