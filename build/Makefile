BASE_DIR      	?= $(abspath .)
SRC_DIR       	?= $(abspath ..)
ARTIFACTS     	?= $(BASE_DIR)/artifacts
BUILD_CONTEXT 	?= $(SRC_DIR)
TEST_DIR		?= $(BASE_DIR)/test


.DEFAULT_GOAL := help

# рецепт целиком в одном shell: без -e упавшая команда в середине не свалит цель
.ONESHELL:
SHELL       := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c


# тег образа: git-тег (если HEAD ровно на теге) либо короткий sha, плюс -dirty на грязном дереве
GIT_TAG := $(shell git -C $(SRC_DIR) describe --tags --exact-match 2>/dev/null)
GIT_SHA := $(shell git -C $(SRC_DIR) rev-parse --short HEAD 2>/dev/null)
DIRTY   := $(shell test -n "$$(git -C $(SRC_DIR) status --porcelain 2>/dev/null)" && echo -dirty)

ifeq ($(strip $(BOBA_TAG)),)
  ifneq ($(strip $(GIT_TAG)),)
    BOBA_TAG := $(GIT_TAG)$(DIRTY)
  else
    BOBA_TAG := $(GIT_SHA)$(DIRTY)
  endif
endif


# версия у всех пакетов одна, лежит в их pyproject.toml (менять: make version-set)
VERSION_FILE := $(SRC_DIR)/packages/core/boba-patterns/pyproject.toml
VERSION      := $(shell sed -n 's/^version = "\(.*\)"/\1/p' $(VERSION_FILE))


PYTHON_VERSION	?= 3.11
# рантайм-база: gcc-образ (glibc+gcc8+libstdc++) + python-дистрибутив поверх
GCC_IMAGE    	?= dmp/gcc:8.5.0
PYTHON_IMAGE 	?= dmp/python:$(PYTHON_VERSION)-dist
# тег с версией python: иначе wheels соберутся на boba-base от прошлой сборки
BASE_IMAGE   	:= boba-base:$(PYTHON_VERSION)
REQUIRES_PY  	:= $(shell sed -n 's/^requires-python = ">=\([0-9]*\.[0-9]*\).*/\1/p' $(VERSION_FILE))
NO_CACHE		?= 0

# OCR-модели tesseract (скачиваются в make sources)
TESS_LANGS    	?= eng rus osd
TESSDATA_REPO 	?= https://github.com/tesseract-ocr/tessdata_best/raw/main
# embedding-веса fastembed (скачиваются в make fastembed)
EMBED_MODELS  	?= intfloat/multilingual-e5-large

# готовый бинарь; для внутреннего mirror переопредели UV_URL
UV_VERSION  	?= 0.11.21
UV_PLATFORM 	?= x86_64-unknown-linux-musl
UV_URL      	?= https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)/uv-$(UV_PLATFORM).tar.gz

IMAGE			:= boba:$(BOBA_TAG)
NO_CACHE_TEXT	:= $(if $(filter 1,$(NO_CACHE)),--no-cache,)

# make extract
RELEASE_NAME 	:= boba-$(VERSION)-$(BOBA_TAG)
OUT          	?= $(BASE_DIR)/extract/$(RELEASE_NAME)
INSTALL_DIR  	?= /opt/boba/$(RELEASE_NAME)
DEPLOY_DIR   	:= $(BASE_DIR)/deploy

# закрытый контур: подхватываем ca-chain.crt, если положили в artifacts
CA_CHAIN := $(wildcard $(ARTIFACTS)/ca-chain.crt)


.PHONY: help
help:
	@echo "boba build — образ $(IMAGE), версия пакетов $(VERSION)"
	echo ""
	echo "Сборка (порядок важен, всё сразу: make all):"
	echo "  sources    скачать tessdata и бинарь uv в artifacts (нужна сеть)"
	echo "  base       собрать boba-base: $(GCC_IMAGE) + $(PYTHON_IMAGE) + uv"
	echo "  wheels     собрать offline-wheelhouse в artifacts/wheels (нужен base, сеть)"
	echo "  fastembed  скачать embedding-веса в artifacts/fastembed (нужны wheels, сеть)"
	echo "  build      собрать $(IMAGE) и boba:latest — offline, из wheelhouse"
	echo "  test       проверить boba:latest (python, uv, пакеты, OCR, magick/soffice/gs)"
	echo ""
	echo "Установка на сервер без docker:"
	echo "  extract    выложить образ в портативную папку с unit и env"
	echo "             OUT=<куда собрать>, INSTALL_DIR=<куда встанет на сервере>"
	echo ""
	echo "Версия (одна на все пакеты, лежит в pyproject.toml):"
	echo "  version        показать"
	echo "  version-set    VER=X.Y.Z — проставить во все пакеты и во внутренние пины"
	echo "  version-check  проверить, что версия одна и пины boba-* совпадают"
	echo ""
	echo "Прочее:"
	echo "  dist       собрать wheel'ы пакетов на хосте в build/dist (без docker)"
	echo "  clean      удалить скачанное в sources (wheels и fastembed остаются)"
	echo "  print-tag  показать тег образа"
	echo ""
	echo "Переменные: BOBA_TAG=<тег>, NO_CACHE=1, PYTHON_VERSION=$(PYTHON_VERSION),"
	echo "            GCC_IMAGE=<ref>, PYTHON_IMAGE=<ref>"
	echo "Пакетам нужен python >=$(REQUIRES_PY); wheels собираются на $(BASE_IMAGE)"
	echo ""


# Все pyproject.toml пакетов; packages/<pkg>/build/ — артефакты сборки, не пакеты.
PYPROJECTS = $(shell find $(SRC_DIR)/packages -name pyproject.toml -not -path '*/build/*')


.PHONY: version
version:
	@echo $(VERSION)


# правит version и пины boba-*==<old> сразу во всех пакетах: врозь их держать нельзя
.PHONY: version-set
version-set:
	@if [ -z "$(VER)" ]; then
	    echo "укажи версию: make version-set VER=X.Y.Z" >&2
	    exit 1
	fi
	if ! echo "$(VER)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+(\.dev[0-9]+)?$$'; then
	    echo "версия должна быть X.Y.Z или X.Y.Z.devN, а не '$(VER)'" >&2
	    exit 1
	fi
	sed -i -e 's/^version = "$(VERSION)"$$/version = "$(VER)"/' \
	       -e 's/"\(boba-[a-z0-9-]*\)==$(VERSION)"/"\1==$(VER)"/g' $(PYPROJECTS)
	echo ">>> $(VERSION) -> $(VER) в $(words $(PYPROJECTS)) пакетах"
	$(MAKE) --no-print-directory version-check VERSION=$(VER)


# ловит ручную правку pyproject.toml: разные version или пин boba-* на чужую версию
.PHONY: version-check
version-check:
	@bad=$$(grep -h '^version = ' $(PYPROJECTS) | sort -u | grep -v '^version = "$(VERSION)"$$' || true)
	if [ -n "$$bad" ]; then
	    echo "версии пакетов разошлись с $(VERSION):" >&2
	    echo "$$bad" >&2
	    exit 1
	fi
	bad=$$(grep -hoE '^[[:space:]]+"boba-[a-z0-9-]+[^"]*"' $(PYPROJECTS) | tr -d '[:blank:]' \
	       | grep -v '^"boba-[a-z0-9-]*==$(VERSION)"$$' | sort -u || true)
	if [ -n "$$bad" ]; then
	    echo "внутренние зависимости не запинены на $(VERSION):" >&2
	    echo "$$bad" >&2
	    exit 1
	fi
	echo "version-check: $(VERSION), $(words $(PYPROJECTS)) пакетов — ok"


.PHONY: debug
debug:
	@echo "BASE_DIR       = $(BASE_DIR)"
	echo "SRC_DIR        = $(SRC_DIR)"
	echo "ARTIFACTS      = $(ARTIFACTS)"
	echo "BUILD_CONTEXT  = $(BUILD_CONTEXT)"
	echo "TEST_DIR       = $(TEST_DIR)"
	echo "BOBA_TAG       = $(BOBA_TAG)"
	echo "VERSION        = $(VERSION)"
	echo "IMAGE          = $(IMAGE)"
	echo "BASE_IMAGE     = $(BASE_IMAGE)"
	echo "GCC_IMAGE      = $(GCC_IMAGE)"
	echo "PYTHON_IMAGE   = $(PYTHON_IMAGE)"
	echo "REQUIRES_PY    = $(REQUIRES_PY)"
	echo "NO_CACHE       = $(NO_CACHE)"
	echo "UV_URL         = $(UV_URL)"
	echo "CA_CHAIN       = $(CA_CHAIN)"
	echo "OUT            = $(OUT)"
	echo "INSTALL_DIR    = $(INSTALL_DIR)"


# единственное, что качается заранее; уже скачанное пропускает
.PHONY: sources
sources:
	@mkdir -p $(ARTIFACTS)/tessdata $(ARTIFACTS)/bin
	echo ">>> OCR-модели tesseract"
	for lang in $(TESS_LANGS); do
	    dest="$(ARTIFACTS)/tessdata/$$lang.traineddata"
	    if [ -f "$$dest" ]; then
	        echo "  ✓ $$dest"
	    else
	        echo ">>> $$lang"
	        curl -fSL -o "$$dest" "$(TESSDATA_REPO)/$$lang.traineddata"
	    fi
	done
	echo ">>> uv ($(UV_PLATFORM))"
	if [ -f "$(ARTIFACTS)/bin/uv" ]; then
	    echo "  ✓ $(ARTIFACTS)/bin/uv"
	else
	    echo ">>> $(UV_URL)"
	    curl -fSL "$(UV_URL)" | tar -xz -C /tmp
	    install -m 0755 /tmp/uv-$(UV_PLATFORM)/uv $(ARTIFACTS)/bin/uv
	    rm -rf /tmp/uv-$(UV_PLATFORM)
	    $(ARTIFACTS)/bin/uv --version
	fi


# образ для сборки wheels и скачивания fastembed: gcc + python + uv, без пакетов
.PHONY: base
base:
	docker build -f Dockerfile --target base $(NO_CACHE_TEXT) \
	    --build-arg GCC_IMAGE=$(GCC_IMAGE) \
	    --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE) \
	    --tag $(BASE_IMAGE) --tag boba-base:latest \
	    --progress plain $(BUILD_CONTEXT)


# без этой проверки pip падает посреди сборки колёс сообщением
# "Package 'boba-xxx' requires a different Python: 3.10.20 not in '<4,>=3.11'",
# где пакет назван случайный — тот, что первым попал в аргументы
.PHONY: check-base
check-base:
	@if [ "$$(printf '%s\n' "$(REQUIRES_PY)" "$(PYTHON_VERSION)" | sort -V | head -1)" != "$(REQUIRES_PY)" ]; then
	    echo "PYTHON_VERSION=$(PYTHON_VERSION), а пакетам нужен python >=$(REQUIRES_PY)" >&2
	    exit 1
	fi
	have=$$(docker run --rm --entrypoint python3 $(BASE_IMAGE) \
	          -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || {
	    echo "нет образа $(BASE_IMAGE) — собери: make base" >&2
	    exit 1
	}
	if [ "$$have" != "$(PYTHON_VERSION)" ]; then
	    echo "$(BASE_IMAGE) собран с python $$have вместо $(PYTHON_VERSION) — пересобери: make base" >&2
	    exit 1
	fi


# колёса всех пакетов (ищутся по pyproject.toml) и их зависимостей.
# каталог не чистится: после удаления зависимости её колесо остаётся —
# чтобы избавиться, пересобрать с нуля (rm -rf artifacts/wheels)
.PHONY: wheels
wheels: version-check check-base
	@echo ">>> версия пакетов: $(VERSION), python $(PYTHON_VERSION)"
	docker run --rm \
	  -e HOST_UID="$$(id -u)" -e HOST_GID="$$(id -g)" \
	  $(if $(CA_CHAIN),-v "$(ARTIFACTS)/ca-chain.crt":/tmp/ca-chain.crt:ro \
	    -e PIP_CERT=/tmp/ca-chain.crt -e REQUESTS_CA_BUNDLE=/tmp/ca-chain.crt -e SSL_CERT_FILE=/tmp/ca-chain.crt) \
	  -v "$(ARTIFACTS)":/artifacts \
	  -v "$(SRC_DIR)":/boba:ro \
	  $(if $(wildcard $(ARTIFACTS)/pip.conf),-v "$(ARTIFACTS)/pip.conf":/etc/pip.conf:ro) \
	  -w /artifacts --entrypoint sh $(BASE_IMAGE) -c '
	    set -e
	    mkdir -p wheels
	    cp -r /boba/packages /tmp/packages
	    pip3 wheel --no-cache-dir \
	      $$(find /tmp/packages -name pyproject.toml -not -path "*/build/*" -printf "%h\n" | sort | tr "\n" " ") \
	      pip setuptools wheel -w wheels/
	    chown -R "$$HOST_UID:$$HOST_GID" wheels'


# веса запекает в образ make build, поэтому запускать до него
.PHONY: fastembed
fastembed: check-base
	@mkdir -p $(ARTIFACTS)/fastembed
	docker run --rm \
	  -e HOST_UID="$$(id -u)" -e HOST_GID="$$(id -g)" \
	  -e EMBED_MODELS="$(EMBED_MODELS)" \
	  -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY -e http_proxy -e https_proxy -e no_proxy \
	  $(if $(CA_CHAIN),-v "$(ARTIFACTS)/ca-chain.crt":/tmp/ca-chain.crt:ro \
	    -e REQUESTS_CA_BUNDLE=/tmp/ca-chain.crt -e CURL_CA_BUNDLE=/tmp/ca-chain.crt) \
	  -v "$(ARTIFACTS)":/artifacts \
	  --entrypoint sh $(BASE_IMAGE) -c '
	    set -e
	    pip3 install --no-cache-dir --no-index --find-links=/artifacts/wheels fastembed
	    for m in $$EMBED_MODELS; do
	      echo ">>> fastembed: $$m"
	      python3 -c "import sys; from fastembed import TextEmbedding;
	        e=TextEmbedding(model_name=sys.argv[1], cache_dir=\"/artifacts/fastembed\");
	        list(e.embed([\"probe\"]))" "$$m" \
	      || echo "!! $$m не скачан — не поддерживается этой версией fastembed"
	    done
	    chown -R "$$HOST_UID:$$HOST_GID" /artifacts/fastembed'


# внешние зависимости offline из wheelhouse, пакеты boba — из исходников
.PHONY: build
build: version-check
	@echo ">>> source:   $(SRC_DIR) @ $(BOBA_TAG)"
	echo ">>> version:  $(VERSION)"
	echo ">>> building: $(IMAGE)  (+ boba:latest)"
	docker build -f Dockerfile $(NO_CACHE_TEXT) \
	  --build-arg GCC_IMAGE=$(GCC_IMAGE) \
	  --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE) \
	  --build-arg BOBA_VERSION="$(VERSION)" \
	  -t $(IMAGE) -t boba:latest \
	  --progress plain $(BUILD_CONTEXT)


# портативная папка для сервера без docker: python с пакетами, веса, модели,
# нужные .so. ОС внутрь не кладём — расчёт на glibc сервера, остальное apt'ом.
#   INSTALL_DIR=<путь>  куда встанет на сервере (пути в unit и env)
#   OUT=<путь>          куда собрать
#   IMAGE=boba:latest   взять готовый образ, а не по текущему тегу
.PHONY: extract
extract:
	@docker image inspect $(IMAGE) >/dev/null 2>&1 || {
	    echo "нет образа $(IMAGE) — собери его (make build) или возьми готовый: make extract IMAGE=boba:latest" >&2
	    exit 1
	}
	rm -rf "$(OUT)"
	# conf/ правят руками, в local/ пишет приложение
	mkdir -p "$(OUT)/conf/prompts" "$(OUT)/local/workspaces/user" \
	         "$(OUT)/local/workspaces/system" "$(OUT)/local/.chainlit"

	echo ">>> собираю артефакты из $(IMAGE)"
	# внутри контейнера образа: он root, поэтому права файлов сохраняются
	docker run --rm -v "$(OUT)":/out --entrypoint sh $(IMAGE) -c '
	    set -eu
	    mkdir -p /out/python/bin /out/python/lib /out/lib /out/opt

	    tar -C /usr/local/lib -cf - python3.11 | tar -C /out/python/lib -xf -
	    cp -a /usr/local/lib/libpython3* /out/python/lib/ 2>/dev/null || true

	    # скрипты пакетов узнаём по python в shebang — так отсеиваются gcc и binutils
	    for f in /usr/local/bin/*; do
	        b=$$(basename "$$f")
	        case "$$b" in
	            python*|pip*|magick) ;;
	            *) head -c 128 "$$f" 2>/dev/null | head -n 1 | grep -q "python" || continue ;;
	        esac
	        cp -a "$$f" /out/python/bin/ 2>/dev/null || true
	    done
	    # shebang ведёт на python образа, которого на сервере нет
	    for f in /out/python/bin/*; do
	        [ -f "$$f" ] || continue
	        if head -c 2 "$$f" 2>/dev/null | grep -q "#!"; then
	            sed -i "1s|^#!.*python[0-9.]*$$|#!/usr/bin/env python3|" "$$f" || true
	        fi
	    done

	    # .so, которых может не быть на сервере; glibc не берём — чужой libc
	    # рядом с системным загрузчиком ломает запуск
	    {
	        ldd /usr/local/bin/python3.11 || true
	        find /out/python/lib/python3.11 -name "*.so" -o -name "*.so.*" | while read -r so; do
	            ldd "$$so" 2>/dev/null || true
	        done
	    } | awk "/=> \//{print \$$3}" | sort -u \
	      | grep -vE "/(ld-linux|libc|libm|libpthread|libdl|librt|libutil|libnsl|libresolv|libcrypt|libanl)[.-]" \
	      | grep -v "^/usr/local/lib/python" \
	      | while read -r lib; do
	            cp -aL "$$lib" /out/lib/ 2>/dev/null || true
	        done
	    cp -aL /usr/local/lib64/libstdc++.so.6 /usr/local/lib64/libgcc_s.so.1 /out/lib/ 2>/dev/null || true

	    tar -C /opt -cf - tessdata fastembed | tar -C /out/opt -xf -

	    chown -R $$(stat -c "%u:%g" /out) /out/python /out/lib /out/opt'

	echo ">>> boba.env (пути релиза)"
	{
	    echo "# Окружение релиза $(RELEASE_NAME), собран из образа $(IMAGE)."
	    echo "# unit подключает файл через EnvironmentFile; руками: set -a; . ./boba.env; set +a"
	    echo ""
	    echo "PYTHONHOME=$(INSTALL_DIR)/python"
	    echo "PATH=$(INSTALL_DIR)/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
	    echo "LD_LIBRARY_PATH=$(INSTALL_DIR)/lib:$(INSTALL_DIR)/python/lib"
	    echo "# без этого в namespace-пакеты (opentelemetry и др.) подмешивается"
	    echo "# ~/.local/lib/pythonX.Y сервера и ломает импорты чужой версией"
	    echo "PYTHONNOUSERSITE=1"
	    echo ""
	    echo "TESSDATA_PREFIX=$(INSTALL_DIR)/opt/tessdata"
	    echo "TESSDATA_PATH=$(INSTALL_DIR)/opt/tessdata"
	    echo "FASTEMBED_CACHE_PATH=$(INSTALL_DIR)/opt/fastembed"
	    echo ""
	    echo "# конфигурация (правится руками) и рабочий каталог (пишет приложение)"
	    echo "BOBA_CONFIG_PATH=$(INSTALL_DIR)/conf/config.toml"
	    echo "CHAINLIT_APP_ROOT=$(INSTALL_DIR)/local"
	    echo ""
	    echo "# сертификаты — системные, серверные"
	    echo "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
	    echo "REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt"
	    echo "CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt"
	    echo "LANG=C.UTF-8"
	} > "$(OUT)/boba.env"

	for f in boba-chainlit.service README.md; do
	    sed -e 's|__INSTALL_DIR__|$(INSTALL_DIR)|g' \
	        -e 's|__RELEASE_NAME__|$(RELEASE_NAME)|g' \
	        -e 's|__IMAGE__|$(IMAGE)|g' \
	        "$(DEPLOY_DIR)/$$f" > "$(OUT)/$$f"
	done
	sed 's|__INSTALL_DIR__|$(INSTALL_DIR)|g' \
	    "$(DEPLOY_DIR)/config.toml.example" > "$(OUT)/conf/config.toml"

	{
	    echo "release: $(RELEASE_NAME)"
	    echo "image:   $(IMAGE)"
	    echo "digest:  $$(docker image inspect -f '{{.Id}}' $(IMAGE))"
	    echo "python:  $(PYTHON_VERSION)"
	    echo "packages version: $(VERSION)"
	} > "$(OUT)/IMAGE.txt"

	echo ""
	echo ">>> релиз готов: $(OUT)  ($$(du -sh --apparent-size "$(OUT)" 2>/dev/null | cut -f1))"
	echo "    1) заполнить $(OUT)/conf/config.toml"
	echo "    2) на сервер: tar -C $$(dirname "$(OUT)") -czf $$(basename "$(OUT)").tar.gz $$(basename "$(OUT)")"
	echo "    3) поставить службу: cp boba-chainlit.service /etc/systemd/system/"
	echo "                        systemctl daemon-reload && systemctl enable --now boba-chainlit"
	echo ""
	echo "    пути в boba.env и unit прописаны на $(INSTALL_DIR)"
	echo "    запуск руками (тот же env, без systemd):"
	echo "      set -a; . boba.env; set +a; python/bin/python3 -m boba.chainlit"


# колёса для раздачи наружу, к образу отношения не имеют.
# --no-sources проверяет, что пакет соберётся и вне workspace
.PHONY: dist
dist: version-check
	@echo ">>> версия пакетов: $(VERSION)"
	rm -rf "$(BASE_DIR)/dist"
	cd "$(SRC_DIR)"
	"$(ARTIFACTS)/bin/uv" build --all-packages --wheel --no-sources -o "$(BASE_DIR)/dist"
	echo ">>> wheels в build/dist:"
	ls -1 "$(BASE_DIR)/dist"


# Функциональная проверка собранного образа boba:latest.
.PHONY: test
test:
	docker run --rm --entrypoint /bin/bash -v $(TEST_DIR):/test:ro boba:latest -euc '
	    echo "== image =="
	    python3 --version
	    uv --version
	    echo "== functional checks =="
	    python3 /test/bobacheck.py'


# Удалить скачанные источники (tessdata + бинарь uv). wheels/fastembed НЕ трогаем.
.PHONY: clean
clean:
	rm -rf $(ARTIFACTS)/tessdata $(ARTIFACTS)/bin/uv


.PHONY: print-tag
print-tag:
	@echo $(BOBA_TAG)


.PHONY: all
all: sources base wheels fastembed build test
