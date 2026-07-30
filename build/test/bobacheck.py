#!/usr/local/bin/python3
"""Проверка работоспособности собранного runtime-образа boba:
интерпретатор, uv, offline-установленные пакеты (boba-* и внешние),
консольные точки входа, OCR-модели и внешние инструменты (magick, soffice, gs)."""
import os
import shutil
import subprocess
import sys

# chainlit на импорте создаёт .chainlit/.files в cwd — уводим в /tmp
os.environ.setdefault("CHAINLIT_APP_ROOT", "/tmp")

failures = []


def check(name, fn):
    try:
        fn()
        print(f"  OK   {name}")
    except Exception as e:  # noqa: BLE001 - тест, ловим всё намеренно
        print(f"  FAIL {name}: {e!r}")
        failures.append(name)


def warn(name, fn):
    try:
        fn()
        print(f"  OK   {name}")
    except Exception as e:  # noqa: BLE001
        print(f"  WARN {name}: {e!r}")


def run(*argv):
    out = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip().splitlines()[-1:])
    return (out.stdout or out.stderr).strip().splitlines()[0]


print("== interpreter ==")
print(f"  python     : {sys.version.split()[0]}")
print(f"  executable : {sys.executable}")

# 1) пакетный менеджер uv (статический бинарь рядом с pip)
print("== package managers ==")
check("uv on PATH", lambda: run("uv", "--version"))
check("pip module", lambda: run(sys.executable, "-m", "pip", "--version"))

# 2) внешние зависимости, поставленные offline из wheelhouse
print("== third-party imports ==")
import importlib

for m in ["pydantic", "fastapi", "tabulate", "plotly", "fastembed"]:
    check(f"import {m}", lambda m=m: importlib.import_module(m))

# 3) все пакеты из /app/packages установлены (без импорта кода — только метаданные).
# Список берём из исходников, а не хардкодим: новый пакет попадает в проверку сам.
print("== boba distributions installed ==")
import importlib.metadata as md
import tomllib

PACKAGES_DIR = "/app/packages"


def package_names():
    names = []
    for root, _dirs, files in os.walk(PACKAGES_DIR):
        if "pyproject.toml" not in files:
            continue
        with open(os.path.join(root, "pyproject.toml"), "rb") as f:
            names.append(tomllib.load(f)["project"]["name"])
    return sorted(names)


required = package_names()
installed = {d.metadata["Name"].lower() for d in md.distributions()}
print(f"  пакетов в {PACKAGES_DIR}: {len(required)}, "
      f"установлено boba-*: {sum(1 for n in installed if n.startswith('boba-'))}")
check(f"пакеты найдены в {PACKAGES_DIR}", lambda: (_ for _ in ()).throw(
    RuntimeError("не найдено ни одного pyproject.toml")) if not required else None)
for dist in required:
    check(f"dist {dist}", lambda dist=dist: md.version(dist))

# 4) консольные точки входа — список берём из метаданных пакетов, не хардкодим
print("== console scripts ==")
scripts = sorted(
    ep.name
    for dist in md.distributions()
    if dist.metadata["Name"].lower().startswith("boba-")
    for ep in dist.entry_points
    if ep.group == "console_scripts"
)
print(f"  объявлено в пакетах: {len(scripts)}")
for script in scripts:
    check(f"which {script}", lambda script=script:
          (_ for _ in ()).throw(RuntimeError("not found"))
          if shutil.which(script) is None else None)

# 5) OCR-модели tesseract (запекаются в /opt/tessdata)
print("== OCR models ==")
tessdir = os.environ.get("TESSDATA_PREFIX", "/opt/tessdata")


def t_tessdata():
    need = ["eng.traineddata", "rus.traineddata", "osd.traineddata"]
    missing = [n for n in need if not os.path.isfile(os.path.join(tessdir, n))]
    assert not missing, f"нет моделей: {missing} в {tessdir}"


check(f"tessdata в {tessdir}", t_tessdata)

# 6) внешние инструменты обработки документов
print("== external tools ==")
check("magick (унифицированный CLI)", lambda: run("magick", "-version"))
warn("soffice (libreoffice)", lambda: run("soffice", "--version"))
warn("gs (ghostscript)", lambda: run("gs", "--version"))

# 7) embedding-веса fastembed — опционально (make fastembed ДО build)
print("== fastembed weights ==")
warn(
    "веса в /opt/fastembed",
    lambda: (_ for _ in ()).throw(RuntimeError("каталог пуст — make fastembed не запускался"))
    if not os.path.isdir("/opt/fastembed") or not os.listdir("/opt/fastembed") else None,
)

print()
if failures:
    print(f"RESULT: FAIL ({len(failures)}): {', '.join(failures)}")
    sys.exit(1)
print("RESULT: ALL OK")
