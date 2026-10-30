#!/bin/bash
# SAST (ruff/bandit/semgrep) и SCA (pip-audit по uv.lock).
# Сканеры ставятся изолированно через uvx: semgrep тянет mcp/pydantic 2.11,
# который несовместим с пином pydantic<2.11 в рабочем окружении.

set -u

_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE:-$0}")/.." 2>/dev/null && pwd)

# semgrep берёт .semgrepignore из корня проверяемого дерева
cd "$_root" || exit 1

if [ -n "${PYTHONHOME:-}" ] || [ -n "${PYTHONPATH:-}" ]; then
  unset PYTHONHOME PYTHONPATH
fi

_uv="$_root/build/src/uv/uv"
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
  "$_root/.venv/bin/ruff" check "$_root/packages" || _rc=1
}

_run_bandit() {
  echo "== bandit =="
  # LOW отфильтрован: там только B404/B603 песочницы, для которой запуск
  # процессов и есть назначение
  "$_uv" tool run --system-certs bandit \
    -q -r "$_root/packages" \
    --exclude '**/tests/**,**/.venv/**,**/boba-catalog-service/**,**/boba-catalog/**' \
    --severity-level medium --confidence-level medium || _rc=1
}

_run_semgrep() {
  echo "== semgrep =="
  # правила скачиваются в sec/semgrep (как остальные артефакты) и в git
  # не попадают: обновляются через sec/sec.sh rules
  # --jobs 1: io_uring semgrep не поднимается при лимите memlock 8 МБ
  SEMGREP_SEND_METRICS=off "$_uv" tool run --system-certs semgrep \
    scan --config="$_root/sec/semgrep" --metrics=off --jobs 1 \
    --exclude=.venv --exclude=build --exclude=release \
    --exclude=boba-catalog-service --exclude=boba-catalog \
    --error "$_root/packages" || _rc=1
}

# охват и правила как у сканера ИБ: всё дерево, а не только packages, плюс
# Dockerfile, Makefile, shell и jsx. Своя часть правил лежит в sec
_run_full() {
  echo "== semgrep: полный охват (правила ИБ) =="
  SEMGREP_SEND_METRICS=off "$_uv" tool run --system-certs semgrep \
    scan --config="$_root/sec/semgrep"                   \
    --config="$_root/sec/semgrep-boba"                  \
    --metrics=off --scan-unknown-extensions --jobs 1 --json -q    \
    "$_root" > "$_full_json" || _rc=1

  python3 - "$_full_json" <<'PY'
import collections
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)

results = report["results"]
by_rule = collections.Counter(r["check_id"].split(".")[-1] for r in results)

print(f"  файлов просканировано: {len(report['paths']['scanned'])}")
print(f"  находок: {len(results)}")
for rule, count in by_rule.most_common():
    print(f"    {count:5}  {rule}")
PY

  echo "== bandit: без порогов, вместе с тестами =="
  "$_uv" tool run --system-certs bandit                     \
    -q -r "$_root/packages" "$_root/build/test"       \
    --exclude '**/.venv/**' -f json 2>/dev/null > "$_bandit_json" || true

  python3 - "$_bandit_json" <<'PY'
import collections
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)

results = report["results"]
by_test = collections.Counter(
    (r["test_id"], r["issue_text"].split(".")[0]) for r in results
)

print(f"  находок: {len(results)}")
for (test, text), count in by_test.most_common():
    print(f"    {count:5}  {test}  {text[:60]}")
PY
}

_update_rules() {
  echo "== semgrep rules =="
  for pack in python secrets dockerfile javascript react; do
    curl -fsS "https://semgrep.dev/c/p/$pack" \
      -o "$_root/sec/semgrep/$pack.yml" || _rc=1
    echo "$pack.yml updated"
  done
}

_run_audit() {
  echo "== pip-audit =="
  local _req
  _req=$(mktemp)
  # хэши не выгружаются: колесо oracledb собрано у нас и хэша в uv.lock не имеет;
  # зависимости в выгрузке уже полные, поэтому pip-audit их не разрешает заново
  "$_uv" export --system-certs --frozen --no-dev --no-emit-workspace --no-hashes \
    --format requirements-txt -o "$_req" --quiet || _rc=1
  # mcp приходит транзитивно от chainlit, свой MCP-сервер проект не поднимает:
  # все три CVE про server transport, а апгрейд упирается в пин pydantic<2.11
  "$_uv" tool run --system-certs pip-audit -r "$_req" --disable-pip --no-deps \
    --ignore-vuln PYSEC-2026-1617 \
    --ignore-vuln PYSEC-2026-3482 \
    --ignore-vuln PYSEC-2026-3483 || _rc=1
  rm -f "$_req"
}

_full_json=$(mktemp)
_bandit_json=$(mktemp)
trap 'rm -f "$_full_json" "$_bandit_json"' EXIT

case "$_mode" in
  full)
    _run_full
    ;;
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
    echo "usage: sec/sec.sh [all|full|sast|sca|rules]" >&2
    exit 2
    ;;
esac

exit $_rc
