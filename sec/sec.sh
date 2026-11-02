#!/bin/bash
# SAST (ruff/bandit/semgrep) и SCA (pip-audit по uv.lock).
# Сканеры запускаются через uv tool в отдельных окружениях, чтобы их
# зависимости не изменяли lock и окружение проверяемого приложения.

set -u

_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE:-$0}")/.." 2>/dev/null && pwd)

# semgrep берёт .semgrepignore из корня проверяемого дерева
cd "$_root" || exit 1

# Окружение запуска приложения может подставлять собственный libstdc++ и Python.
# Сканеры используют библиотеки хоста, а не проверяемой поставки.
unset PYTHONHOME PYTHONPATH LD_LIBRARY_PATH LD_PRELOAD

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
    --exclude '**/tests/**,**/.venv/**' \
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

  python3 - "$_full_json" <<'PY' || _rc=1
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
  local _bandit_status=0
  "$_uv" tool run --system-certs bandit                     \
    -q -r "$_root/packages" "$_root/build/test"       \
    --exclude '**/.venv/**' -f json 2>/dev/null > "$_bandit_json" || _bandit_status=$?
  if [ "$_bandit_status" -gt 1 ]; then
    _rc=1
  fi

  python3 - "$_bandit_json" <<'PY' || _rc=1
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
  # хэши не выгружаются: локальные колёса собраны у нас и хэша в uv.lock не имеют;
  # зависимости в выгрузке уже полные, поэтому pip-audit их не разрешает заново
  # Extras содержат production-провайдеры (OCR, readers, ONNX), а не только
  # разработку. Проверяем весь workspace lock, включая дополнительные наборы.
  if ! "$_uv" export --system-certs --frozen --all-packages --all-extras \
    --no-dev --no-emit-workspace --no-hashes \
    --format requirements-txt -o "$_req" --quiet; then
    rm -f "$_req"
    _rc=1
    return
  fi
  "$_uv" tool run --system-certs pip-audit -r "$_req" --disable-pip --no-deps || _rc=1

  # PyPI не знает локальных версий. Отдельно проверяем их upstream из lock;
  # это проверка известных CVE исходной версии, не сертификация нашего патча.
  echo "== pip-audit: upstream локальных патчей =="
  if python3 - > "$_req" <<'PY'
import pathlib
import tomllib

lock = tomllib.loads(pathlib.Path("uv.lock").read_text())
for package in lock["package"]:
    version, suffix, _ = package["version"].partition("+boba.")
    if suffix:
        print(f"{package['name']}=={version}")
PY
  then
    if [ -s "$_req" ]; then
      "$_uv" tool run --system-certs pip-audit -r "$_req" --disable-pip --no-deps || _rc=1
    fi
  else
    _rc=1
  fi
  rm -f "$_req"
}

umask 077
if [ -n "${SEC_REPORT_DIR:-}" ]; then
  mkdir -p "$SEC_REPORT_DIR" || exit 1
  _full_json="$SEC_REPORT_DIR/semgrep.json"
  _bandit_json="$SEC_REPORT_DIR/bandit.json"
else
  _full_json=$(mktemp) || exit 1
  _bandit_json=$(mktemp) || exit 1
  trap 'rm -f "$_full_json" "$_bandit_json"' EXIT
fi

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
