#!/bin/bash
# SAST (ruff/bandit/semgrep) и SCA (pip-audit по uv.lock).
# Сканеры ставятся изолированно через uvx: semgrep тянет mcp/pydantic 2.11,
# который несовместим с пином pydantic<2.11 в рабочем окружении.

set -u

_sec_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE:-$0}")" 2>/dev/null && pwd)

if [ -n "${PYTHONHOME:-}" ] || [ -n "${PYTHONPATH:-}" ]; then
  unset PYTHONHOME PYTHONPATH
fi

_uv="$_sec_dir/build/src/uv/uv"
if [ ! -x "$_uv" ]; then
  _uv=$(command -v uv)
fi

if [ -z "$_uv" ]; then
  echo "sec: не найден uv (build/src/uv/uv или в PATH)" >&2
  exit 1
fi

_mode=${1:-all}
_rc=0

_run_ruff() {
  echo "== ruff (flake8-bandit S + общий линт) =="
  "$_sec_dir/.venv/bin/ruff" check "$_sec_dir/packages" || _rc=1
}

_run_bandit() {
  echo "== bandit =="
  # LOW отфильтрован: там только B404/B603 песочницы, для которой запуск
  # процессов и есть назначение
  "$_uv" tool run --system-certs bandit \
    -q -r "$_sec_dir/packages" \
    --exclude '**/tests/**,**/.venv/**' \
    --severity-level medium --confidence-level medium || _rc=1
}

_run_semgrep() {
  echo "== semgrep =="
  # правила скачиваются в build/src (как остальные артефакты) и в git
  # не попадают: обновляются через sec.sh rules
  SEMGREP_SEND_METRICS=off "$_uv" tool run --system-certs semgrep \
    scan --config="$_sec_dir/build/src/semgrep" --metrics=off \
    --exclude=.venv --exclude=build --exclude=release \
    --error "$_sec_dir/packages" || _rc=1
}

_update_rules() {
  echo "== semgrep rules =="
  for pack in python secrets; do
    curl -fsS "https://semgrep.dev/c/p/$pack" \
      -o "$_sec_dir/build/src/semgrep/$pack.yml" || _rc=1
    echo "$pack.yml updated"
  done
}

_run_audit() {
  echo "== pip-audit =="
  local _req
  _req=$(mktemp)
  "$_uv" export --system-certs --frozen --no-dev --no-emit-workspace \
    --format requirements-txt -o "$_req" --quiet || _rc=1
  # mcp приходит транзитивно от chainlit, свой MCP-сервер проект не поднимает:
  # все три CVE про server transport, а апгрейд упирается в пин pydantic<2.11
  "$_uv" tool run --system-certs pip-audit -r "$_req" --disable-pip \
    --ignore-vuln PYSEC-2026-1617 \
    --ignore-vuln PYSEC-2026-3482 \
    --ignore-vuln PYSEC-2026-3483 || _rc=1
  rm -f "$_req"
}

case "$_mode" in
  sast)
    _run_ruff
    _run_bandit
    _run_semgrep
    ;;
  sca)
    _run_audit
    ;;
  all)
    _run_ruff
    _run_bandit
    _run_semgrep
    _run_audit
    ;;
  rules)
    _update_rules
    ;;
  *)
    echo "usage: sec.sh [all|sast|sca|rules]" >&2
    exit 2
    ;;
esac

exit $_rc
