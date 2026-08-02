# сборщик boba: портативный релиз из локального дерева пакетов.
# Онлайн шаги: sources + deps + build (зависимости приезжают из nexus).
# extract — портативная папка из готового образа.

BASE_DIR      	?= $(abspath .)
SRC_DIR       	?= $(abspath ..)
ARTIFACTS     	?= $(BASE_DIR)/artifacts
BUILD_CONTEXT 	?= $(SRC_DIR)
TEST_DIR      	?= $(BASE_DIR)/test
CONF_DIR      	:= $(BASE_DIR)/conf

# исходники, которые собираются внутри Dockerfile
SRC_ARTIFACTS 	:= $(ARTIFACTS)/src
# бинарные зависимости, которые внутри Dockerfile не достать
DEPS_DIR      	?= $(ARTIFACTS)


.DEFAULT_GOAL := help

# рецепт целиком в одном shell: без -e упавшая команда в середине не свалит цель
.ONESHELL:
SHELL       := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c


# версия у всех пакетов одна, лежит в их pyproject.toml (менять: make version-set)
VERSION_FILE := $(SRC_DIR)/packages/core/boba-patterns/pyproject.toml
VERSION      := $(shell sed -n 's/^version = "\(.*\)"/\1/p' $(VERSION_FILE))
BOBA_TAG     ?= $(VERSION)
REQUIRES_PY  := $(shell sed -n 's/^requires-python = ">=\([0-9]*\.[0-9]*\).*/\1/p' $(VERSION_FILE))

# портативный релиз: на сервере лежит в DEST_DIR, этот путь зашивается в образ
# (build-arg BOBA_DIR) и в conf/*.  Перенести релиз в другое место —
# пересобрать: make build DEST_DIR=<путь>
RELEASE_NAME  := boba-$(VERSION)
DEST_DIR      ?= /app/boba/$(VERSION)

# финальный runtime-образ
IMAGE         := boba:$(BOBA_TAG)
# из какого образа make extract достаёт портативную папку и куда её кладёт
RELEASE_IMAGE ?= $(IMAGE)
OUT           ?= $(ARTIFACTS)/build/$(RELEASE_NAME)

PYTHON_VERSION	?= 3.11
# чистая рантайм-база (только glibc), от неё строится финальный образ runtime
GLIBC_IMAGE  	?= dmp/glibc:2.28
# среда сборки: glibc 2.28 + gcc + python-дистрибутив
GCC_IMAGE    	?= dmp/gcc:8.5.0
PYTHON_IMAGE 	?= dmp/python:$(PYTHON_VERSION)-dist
# образ, чья файловая система становится rootfs песочницы
SANDBOX_IMAGE	?= dmp/python:$(PYTHON_VERSION)
# тег с версией python: иначе deps соберутся на boba-base от прошлой сборки
BASE_IMAGE    := boba-base:$(PYTHON_VERSION)

# OCR-модели tesseract
TESS_LANGS    	?= eng rus osd
TESSDATA_REPO 	?= https://github.com/tesseract-ocr/tessdata_best/raw/main
# embedding-веса fastembed
EMBED_MODELS  	?= intfloat/multilingual-e5-large

# готовый бинарь; для внутреннего mirror переопредели UV_URL
UV_VERSION  	?= 0.11.21
UV_PLATFORM 	?= x86_64-unknown-linux-musl
UV_URL      	?= https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)/uv-$(UV_PLATFORM).tar.gz

# bubblewrap: собирается стадией bwrap-build
BWRAP_VERSION	?= 0.11.2
BWRAP_URL     	?= https://github.com/containers/bubblewrap/releases/download/v$(BWRAP_VERSION)/bubblewrap-$(BWRAP_VERSION).tar.xz
BWRAP_TARBALL 	:= $(SRC_ARTIFACTS)/bubblewrap-$(BWRAP_VERSION).tar.xz

# сбросить ли кэш сборки: docker build --no-cache
NO_CACHE		?= 0
NO_CACHE_OPT	:= $(if $(filter 1,$(NO_CACHE)),--no-cache,)

# --progress есть только у buildkit; на старом docker флага нет — не подставляем
PROGRESS      ?= $(shell docker build --help 2>/dev/null | grep -q -- '--progress' && echo '--progress plain')

# закрытый контур: подхватываем ca-chain.crt, если положили в artifacts
CA_CHAIN := $(wildcard $(ARTIFACTS)/ca-chain.crt)

# общие build-arg: образы тулчейна и версии
DOCKER_ARGS = --build-arg GLIBC_IMAGE=$(GLIBC_IMAGE)         \
              --build-arg GCC_IMAGE=$(GCC_IMAGE)             \
              --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE)       \
              --build-arg PYTHON_VERSION=$(PYTHON_VERSION)   \
              --build-arg BWRAP_VERSION=$(BWRAP_VERSION)


.PHONY: help
help:
	@echo "Сборка boba $(VERSION) из дерева пакетов $(SRC_DIR) -> $(IMAGE)"
	echo ""
	echo "  make sources   исходники, собираемые внутри Dockerfile -> $(SRC_ARTIFACTS)"
	echo "  make deps      бинарные зависимости -> $(DEPS_DIR)/{uv,tessdata,fastembed,sandbox-rootfs}"
	echo "  make build     собрать образ $(IMAGE)"
	echo "  make test      проверить релиз внутри образа"
	echo "  make extract   портативная папка из готового образа:"
	echo "                 RELEASE_IMAGE=$(RELEASE_IMAGE), OUT=$(OUT)"
	echo "  make clean     удалить исходники, deps и собранную папку (кроме fastembed)"
	echo ""
	echo "  make all       sources + deps + build + test"
	echo ""
	echo "  Версия (одна на все пакеты, лежит в pyproject.toml):"
	echo "    version        показать"
	echo "    version-set    VER=X.Y.Z — проставить во все пакеты и во внутренние пины"
	echo "    version-check  проверить, что версия одна и пины boba-* совпадают"
	echo ""
	echo "  Nexus для сторонних колёс: положить conf/pip.conf и conf/uv.toml"
	echo "  (примеры рядом), они копируются в образ как есть."
	echo ""
	echo "  Переменные: DEST_DIR=$(DEST_DIR), BOBA_TAG=<тег>, NO_CACHE=1,"
	echo "              PYTHON_VERSION=$(PYTHON_VERSION), GLIBC_IMAGE=$(GLIBC_IMAGE),"
	echo "              GCC_IMAGE=$(GCC_IMAGE), PYTHON_IMAGE=$(PYTHON_IMAGE)"
	echo "  Пакетам нужен python >=$(REQUIRES_PY)"


.PHONY: debug
debug:
	@true
	$(info BASE_DIR       = $(BASE_DIR))
	$(info SRC_DIR        = $(SRC_DIR))
	$(info ARTIFACTS      = $(ARTIFACTS))
	$(info SRC_ARTIFACTS  = $(SRC_ARTIFACTS))
	$(info DEPS_DIR       = $(DEPS_DIR))
	$(info TEST_DIR       = $(TEST_DIR))
	$(info VERSION        = $(VERSION))
	$(info RELEASE_NAME   = $(RELEASE_NAME))
	$(info IMAGE          = $(IMAGE))
	$(info RELEASE_IMAGE  = $(RELEASE_IMAGE))
	$(info OUT            = $(OUT))
	$(info DEST_DIR       = $(DEST_DIR))
	$(info BASE_IMAGE     = $(BASE_IMAGE))
	$(info GLIBC_IMAGE    = $(GLIBC_IMAGE))
	$(info GCC_IMAGE      = $(GCC_IMAGE))
	$(info PYTHON_IMAGE   = $(PYTHON_IMAGE))
	$(info SANDBOX_IMAGE  = $(SANDBOX_IMAGE))
	$(info REQUIRES_PY    = $(REQUIRES_PY))
	$(info NO_CACHE       = $(NO_CACHE))
	$(info UV_URL         = $(UV_URL))
	$(info CA_CHAIN       = $(CA_CHAIN))


# ---------------------------------------------------------------------------
# Версия пакетов
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# sources: исходники, которые собираются стадиями Dockerfile
# ---------------------------------------------------------------------------

# bubblewrap: apt в Astra 2.12 даёт 0.3.3 (нет --clearenv и части unshare-флагов),
# поэтому свежий собирается из исходников стадией bwrap-build
.PHONY: sources
sources:
	@mkdir -p $(SRC_ARTIFACTS)
	echo "== bubblewrap $(BWRAP_VERSION) =="
	[ -f "$(BWRAP_TARBALL)" ] || curl -fSL -o "$(BWRAP_TARBALL)" "$(BWRAP_URL)"
	ls -lh $(SRC_ARTIFACTS)/


# ---------------------------------------------------------------------------
# deps: бинарные зависимости, которые внутри Dockerfile не достать
# ---------------------------------------------------------------------------

.PHONY: deps
deps: uv tessdata sandbox-rootfs fastembed
	@du -sh $(DEPS_DIR)/uv $(DEPS_DIR)/tessdata $(DEPS_DIR)/fastembed $(DEPS_DIR)/sandbox-rootfs


# uv нужен раньше остальных: он ставится в образ base
.PHONY: uv
uv:
	@mkdir -p $(DEPS_DIR)/uv
	echo "== uv $(UV_VERSION) ($(UV_PLATFORM)) =="
	if [ -f "$(DEPS_DIR)/uv/uv" ]; then
	    echo "  ✓ $(DEPS_DIR)/uv/uv"
	else
	    curl -fSL "$(UV_URL)" | tar -xz -C /tmp
	    install -m 0755 /tmp/uv-$(UV_PLATFORM)/uv $(DEPS_DIR)/uv/uv
	    rm -rf /tmp/uv-$(UV_PLATFORM)
	    $(DEPS_DIR)/uv/uv --version
	fi


.PHONY: tessdata
tessdata:
	@mkdir -p $(DEPS_DIR)/tessdata
	echo "== OCR-модели tesseract: $(TESS_LANGS) =="
	for lang in $(TESS_LANGS); do
	    dest="$(DEPS_DIR)/tessdata/$$lang.traineddata"
	    if [ -f "$$dest" ]; then
	        echo "  ✓ $$dest"
	    else
	        curl -fSL -o "$$dest" "$(TESSDATA_REPO)/$$lang.traineddata"
	    fi
	done


# rootfs для песочницы: read-only корень bwrap — файловая система
# $(SANDBOX_IMAGE) как есть. Python, bash и coreutils уже внутри образа,
# поэтому ничего не копируем и не настраиваем: только /workspace, который
# bwrap не создаст сам на read-only корне.
.PHONY: sandbox-rootfs
sandbox-rootfs:
	@dest="$(DEPS_DIR)/sandbox-rootfs"
	echo "== rootfs песочницы из $(SANDBOX_IMAGE) =="
	if [ -n "$$(ls -A "$$dest" 2>/dev/null)" ]; then
	    echo "  ✓ $$dest (пересобрать: rm -rf $$dest)"
	    exit 0
	fi
	mkdir -p "$$dest"
	cid=$$(docker create $(SANDBOX_IMAGE) /bin/true)
	docker export "$$cid" | tar -x -C "$$dest"
	docker rm "$$cid" >/dev/null
	mkdir -p "$$dest/workspace"


# веса запекает в образ make build, поэтому качать до него.
# fastembed ставится из nexus по conf/pip.conf, зашитому в образ base
.PHONY: fastembed
fastembed: base
	@mkdir -p $(DEPS_DIR)/fastembed
	echo "== веса fastembed: $(EMBED_MODELS) =="
	docker run --rm \
	  -e HOST_UID="$$(id -u)" -e HOST_GID="$$(id -g)" \
	  -e EMBED_MODELS="$(EMBED_MODELS)" \
	  -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY -e http_proxy -e https_proxy -e no_proxy \
	  $(if $(CA_CHAIN),-v "$(ARTIFACTS)/ca-chain.crt":/tmp/ca-chain.crt:ro \
	    -e REQUESTS_CA_BUNDLE=/tmp/ca-chain.crt -e CURL_CA_BUNDLE=/tmp/ca-chain.crt) \
	  -v "$(DEPS_DIR)/fastembed":/fastembed \
	  --entrypoint sh $(BASE_IMAGE) -c '
	    set -e
	    pip3 install --no-cache-dir fastembed
	    for m in $$EMBED_MODELS; do
	      echo ">>> fastembed: $$m"
	      python3 -c "import sys; from fastembed import TextEmbedding;
	        e=TextEmbedding(model_name=sys.argv[1], cache_dir=\"/fastembed\");
	        list(e.embed([\"probe\"]))" "$$m" \
	      || echo "!! $$m не скачан — не поддерживается этой версией fastembed"
	    done
	    chown -R "$$HOST_UID:$$HOST_GID" /fastembed'


# среда сборки: gcc + python + uv + внешние программы (apt).
# Эта же стадия — основа сборочных стадий Dockerfile и контейнер для fastembed.
.PHONY: base
base: uv
	@if [ "$$(printf '%s\n' "$(REQUIRES_PY)" "$(PYTHON_VERSION)" | sort -V | head -1)" != "$(REQUIRES_PY)" ]; then
	    echo "PYTHON_VERSION=$(PYTHON_VERSION), а пакетам нужен python >=$(REQUIRES_PY)" >&2
	    exit 1
	fi
	docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) \
	    $(DOCKER_ARGS)                                     \
	    --target base                                      \
	    --tag $(BASE_IMAGE) --tag boba-base:latest         \
	    $(PROGRESS) $(BUILD_CONTEXT)


# ---------------------------------------------------------------------------
# build: образ. Колёса пакетов boba собираются стадией deps Dockerfile,
# сторонние зависимости тянутся из nexus (conf/pip.conf, conf/uv.toml).
# ---------------------------------------------------------------------------

.PHONY: check-artifacts
check-artifacts:
	@[ -f "$(BWRAP_TARBALL)" ] || {
	    echo "нет исходников bubblewrap — запусти: make sources" >&2; exit 1
	}
	[ -f "$(DEPS_DIR)/uv/uv" ] || {
	    echo "нет бинаря uv — запусти: make deps" >&2; exit 1
	}
	[ -f "$(DEPS_DIR)/tessdata/eng.traineddata" ] || {
	    echo "нет моделей OCR — запусти: make deps" >&2; exit 1
	}
	[ -n "$$(ls -A "$(DEPS_DIR)/fastembed" 2>/dev/null)" ] || {
	    echo "нет весов fastembed — запусти: make deps" >&2; exit 1
	}


.PHONY: build
build: version-check check-artifacts
	@echo ">>> образ $(IMAGE), релиз внутри него лежит в $(DEST_DIR)"
	docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) \
	    $(DOCKER_ARGS)                                     \
	    --target runtime                                   \
	    --build-arg BOBA_DIR="$(DEST_DIR)"                 \
	    --tag $(IMAGE) --tag boba:latest                   \
	    $(PROGRESS) $(BUILD_CONTEXT)


# Функциональная проверка релиза внутри образа: там стоят системные библиотеки
# (как на сервере после apt install), которых на хосте сборки может не быть.
.PHONY: test
test: build
	@echo "== проверка $(RELEASE_NAME) в образе $(IMAGE) =="
	docker run --rm \
	  -v "$(TEST_DIR)":/test:ro \
	  --entrypoint sh $(IMAGE) -c '
	    set -a && . "$(DEST_DIR)/conf/boba.env" && set +a
	    "$$PYTHONHOME/bin/python3" /test/bobacheck.py --names /tmp/names.txt
	  '


# ---------------------------------------------------------------------------
# extract: портативная папка из готового образа — её и кладут на сервер
# ---------------------------------------------------------------------------

.PHONY: extract
extract:
	@docker image inspect $(RELEASE_IMAGE) >/dev/null 2>&1 || {
	    echo "нет образа $(RELEASE_IMAGE) — собери: make build" >&2; exit 1
	}
	rm -rf "$(OUT)"
	mkdir -p "$(OUT)"
	cid=$$(docker create --entrypoint /bin/sh $(RELEASE_IMAGE))
	trap 'docker rm -f "$$cid" >/dev/null 2>&1 || true' EXIT
	docker cp "$$cid:$(DEST_DIR)/." "$(OUT)/"
	docker rm "$$cid" >/dev/null
	trap - EXIT

	# текстовые шаблоны — envsubst с путём релиза ${BOBA_DIR}. В config.toml
	# остаются TOML-интерполяции (${agent.default}, ...): envsubst с явным
	# списком переменных их не трогает.
	export BOBA_DIR="$(DEST_DIR)" PYTHON_VERSION="$(PYTHON_VERSION)"
	envsubst '$${BOBA_DIR}' \
	    < "$(CONF_DIR)/boba-chainlit.service" > "$(OUT)/conf/boba-chainlit.service"
	envsubst '$${BOBA_DIR} $${PYTHON_VERSION}' \
	    < "$(CONF_DIR)/boba.env" > "$(OUT)/conf/boba.env"
	envsubst '$${BOBA_DIR}' \
	    < "$(CONF_DIR)/config.toml.example" > "$(OUT)/conf/config.toml"

	{
	    echo "release: $(RELEASE_NAME)"
	    echo "version: $(VERSION)"
	    echo "python:  $(PYTHON_VERSION)"
	    echo "image:   $(RELEASE_IMAGE)"
	    echo "dest:    $(DEST_DIR)"
	} > "$(OUT)/VERSION.txt"

	echo ">>> релиз готов: $(OUT)  ($$(du -sh --apparent-size "$(OUT)" 2>/dev/null | cut -f1))"
	echo "    на сервере он должен лежать в $(DEST_DIR) — путь зашит в conf/"


# fastembed не трогаем: веса качаются долго
.PHONY: clean
clean:
	rm -rf $(SRC_ARTIFACTS) $(ARTIFACTS)/build \
	       $(DEPS_DIR)/uv $(DEPS_DIR)/tessdata $(DEPS_DIR)/sandbox-rootfs


.PHONY: print-tag
print-tag:
	@echo $(BOBA_TAG)


.PHONY: all
all: sources deps build test
