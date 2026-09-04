# Сборка boba-studio. Сеть нужна только цели fetch: она складывает всё в src/,
# остальные цели работают offline из src/ и локальных образов.

# от каталога Makefile, а не от cwd: make -f build/Makefile из корня искал бы
# test/ и conf/ рядом с собой
BASE_DIR        ?= $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
SRC_DIR         ?= $(abspath $(BASE_DIR)/../..)
# вложенные вызовы: make -f build/Makefile не подставляет его в $(MAKE)
MAKEFILE        := $(BASE_DIR)/Makefile
BUILD_CONTEXT   ?= $(SRC_DIR)
TEST_DIR        := $(BASE_DIR)/test
SCRIPTS_DIR     := $(BASE_DIR)/scripts
CONF_DIR        := $(BASE_DIR)/conf
# артефакты для сборки: tarball'ы, uv, tessdata, модели, webassets, sandbox
DEPS_DIR        ?= $(BASE_DIR)/src
RELEASE_DIR     := $(SRC_DIR)/release

APP             := studio
APP_PACKAGES    := boba-studio[tools]
APP_IMPORTS     := boba.studio
APP_HEALTH      := /api/openapi.json
APP_CGROUP      := boba-sandbox-studio

.DEFAULT_GOAL := help

# рецепт целиком в одном shell: без -e упавшая команда в середине не свалит цель
.ONESHELL:
SHELL           := /bin/bash
.SHELLFLAGS     := -eu -o pipefail -c

# версия у всех пакетов одна, лежит в их pyproject.toml (менять: make version-set)
PYTHON          ?= python3
VERSION         := $(shell $(PYTHON) $(SCRIPTS_DIR)/version.py show $(SRC_DIR)/packages)
REQUIRES_PY     := $(shell $(PYTHON) $(SCRIPTS_DIR)/version.py requires $(SRC_DIR)/packages)
BOBA_TAG        ?= $(VERSION)

# корень установки на сервере и данные, переживающие релизы; тот же корень в образе
INSTALL_DIR     ?= /app/boba-$(APP)
DATA_DIR        ?= $(INSTALL_DIR)/data
DEST_DIR        ?= /app/boba

IMAGE           := boba-$(APP):$(BOBA_TAG)
RELEASE_IMAGE   ?= $(IMAGE)
OUT             ?= $(RELEASE_DIR)/$(VERSION)/$(APP)

# режим sec.sh: all, sast, sca или rules
MODE            ?= all

PYTHON_VERSION  ?= 3.11
GLIBC_IMAGE     ?= dmp/glibc:2.28
GCC_IMAGE       ?= dmp/gcc:8.5.0
PYTHON_IMAGE    ?= dmp/python:$(PYTHON_VERSION)-dist
NODE_IMAGE      ?= dmp/nodejs:22
BASE_IMAGE      := boba-base-$(APP):$(PYTHON_VERSION)
BWRAP_IMAGE     := boba-bwrap-$(APP):$(PYTHON_VERSION)
FUSE_IMAGE      := boba-fuse2fs-$(APP):$(PYTHON_VERSION)
SANDBOX_DIR     := $(DEPS_DIR)/sandbox
# нативное окружение liteparse: office/растр/pdf он конвертирует этими программами

# --- что скачивает fetch -------------------------------------------------------

UV_VERSION      ?= 0.11.21
UV_PLATFORM     ?= x86_64-unknown-linux-musl
UV_URL          ?= https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)/uv-$(UV_PLATFORM).tar.gz
UV_BIN          := $(DEPS_DIR)/uv/uv

BWRAP_VERSION   ?= 0.11.2
BWRAP_URL       ?= https://github.com/containers/bubblewrap/releases/download/v$(BWRAP_VERSION)/bubblewrap-$(BWRAP_VERSION).tar.xz
BWRAP_TARBALL   := $(DEPS_DIR)/bubblewrap-$(BWRAP_VERSION).tar.xz
# 3.12.0 — последний libfuse, которому хватает meson 0.49 из apt Astra 2.12
LIBFUSE_VERSION ?= 3.12.0
LIBFUSE_URL     ?= https://github.com/libfuse/libfuse/releases/download/fuse-$(LIBFUSE_VERSION)/fuse-$(LIBFUSE_VERSION).tar.xz
LIBFUSE_TARBALL := $(DEPS_DIR)/fuse-$(LIBFUSE_VERSION).tar.xz
E2FSPROGS_VERSION ?= 1.47.2
E2FSPROGS_URL   ?= https://www.kernel.org/pub/linux/kernel/people/tytso/e2fsprogs/v$(E2FSPROGS_VERSION)/e2fsprogs-$(E2FSPROGS_VERSION).tar.xz
E2FSPROGS_TARBALL := $(DEPS_DIR)/e2fsprogs-$(E2FSPROGS_VERSION).tar.xz
SANDBOX_WS_SIZE ?= 1G

TESS_LANGS      ?= eng rus osd
TESSDATA_REPO   ?= https://github.com/tesseract-ocr/tessdata_best/raw/main
TESS_FILES      := $(TESS_LANGS:%=$(DEPS_DIR)/tessdata/%.traineddata)

# веса эмбеддера: версия качалки фиксируется, иначе приедет произвольный набор моделей
FASTEMBED_VERSION ?= 0.8.0
EMBED_MODELS    ?= intfloat/multilingual-e5-large
FASTEMBED_STAMPS := $(foreach m,$(EMBED_MODELS),$(DEPS_DIR)/fastembed/.$(subst /,--,$(m)))

# статика фронта: KaTeX (формулы), Inter (шрифт UI), mermaid (диаграммы) вместо CDN
WEBASSETS_DIR   := $(DEPS_DIR)/webassets
KATEX_VERSION   ?= 0.16.22
KATEX_URL       ?= https://github.com/KaTeX/KaTeX/releases/download/v$(KATEX_VERSION)/katex.tar.gz
INTER_VERSION   ?= 5.3.0
INTER_URL       ?= https://registry.npmjs.org/@fontsource/inter/-/inter-$(INTER_VERSION).tgz
INTER_WEIGHTS   ?= 400 500 700
MERMAID_VERSION ?= 11.16.1
MERMAID_URL     ?= https://registry.npmjs.org/mermaid/-/mermaid-$(MERMAID_VERSION).tgz
WEB_FILES       := $(WEBASSETS_DIR)/katex/katex.min.css $(WEBASSETS_DIR)/inter/inter.css $(WEBASSETS_DIR)/mermaid/mermaid.min.js

INPUTS          := $(BWRAP_TARBALL) $(LIBFUSE_TARBALL) $(E2FSPROGS_TARBALL) $(UV_BIN) \
                   $(TESS_FILES) $(FASTEMBED_STAMPS) $(WEB_FILES)

# что кладёт цель sandbox: нужно dev-дереву и релизу
SANDBOX_PARTS   := $(SANDBOX_DIR)/third/bin/bwrap $(SANDBOX_DIR)/third/bin/fuse2fs $(SANDBOX_DIR)/workspace.ext4

# пакеты репозитория: их src едут в песочницу dev-дерева биндом
PYPROJECTS      := $(shell find $(SRC_DIR)/packages -name pyproject.toml -not -path '*/build/*')
PACKAGE_SRCS    := $(wildcard $(PYPROJECTS:%/pyproject.toml=%/src))

# страница workflow: собирается node'ом образа, на хосте node не нужен
WEB_DIR         := $(SRC_DIR)/packages/agents/boba-studio
WEB_PROJECT     := $(WEB_DIR)/web/workflow
VENV_PYTHON     ?= $(SRC_DIR)/.venv/bin/python
WEB_DEV_NAME    ?= boba-web-dev
WEB_DEV_PORT    ?= 5173
BOBA_URL_PREFIX ?= /boba-studio-debug
WEB_DEV_URL      = http://127.0.0.1:$(WEB_DEV_PORT)$(BOBA_URL_PREFIX)/workflow-dev/
WEB_DEV_WAIT    ?= 180

# --- docker ---------------------------------------------------------------------

NO_CACHE        ?= 0
NO_CACHE_OPT    := $(if $(filter 1,$(NO_CACHE)),--no-cache,)
# --progress есть только у buildkit
PROGRESS        ?= $(shell docker build --help 2>/dev/null | grep -q -- '--progress' && echo '--progress plain')

# закрытый контур: CA внутреннего TLS для curl хоста; в образы его кладёт Dockerfile из conf/common
CA_CHAIN        := $(wildcard $(CONF_DIR)/ca-chain.crt)
CURL_OPTS       := $(if $(CA_CHAIN),--cacert "$(CA_CHAIN)")

# прокси: в docker build аргументами, в docker run окружением
PROXY_VARS      := http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY
PROXY_ARGS      := $(foreach v,$(PROXY_VARS),$(if $($(v)),--build-arg $(v)="$($(v))"))
PROXY_ENV       := $(foreach v,$(PROXY_VARS),-e $(v))

DOCKER_ARGS      = --build-arg GLIBC_IMAGE=$(GLIBC_IMAGE)              	\
    --build-arg GCC_IMAGE=$(GCC_IMAGE)                                 	\
    --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE)                           	\
    --build-arg NODE_IMAGE=$(NODE_IMAGE)                               	\
    --build-arg PYTHON_VERSION=$(PYTHON_VERSION)                       	\
    --build-arg BWRAP_VERSION=$(BWRAP_VERSION)                         	\
    --build-arg LIBFUSE_VERSION=$(LIBFUSE_VERSION)                     	\
    --build-arg E2FSPROGS_VERSION=$(E2FSPROGS_VERSION)                 	\
    --build-arg SANDBOX_WS_SIZE=$(SANDBOX_WS_SIZE)                     	\

# аргументы стадий одного приложения
APP_ARGS         = --build-arg APP=$(APP)                              	\
    --build-arg APP_PACKAGES="$(APP_PACKAGES)"                      	\
    --build-arg APP_IMPORTS="$(APP_IMPORTS)"                        	\
    --build-arg HEALTH_PATH="$(APP_HEALTH)"                         	\
    --build-arg SANDBOX_DEV_KRB=$(SRC_DIR)/compose/$(APP)/conf/krb     	\
    --build-arg BOBA_DIR="$(DEST_DIR)" --build-arg BOBA_VERSION="$(VERSION)"

# сборка стадии: $(1) — стадия, $(2) — тег, $(3) — дополнительные аргументы
define docker-stage
docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) $(DOCKER_ARGS) $(PROXY_ARGS) $(3) \
    --target $(1) --tag $(2) $(PROGRESS) $(BUILD_CONTEXT)
endef

# загрузка: $(1) — url, $(2) — файл
define fetch
mkdir -p "$(dir $(2))"
curl -fSL $(CURL_OPTS) -o "$(2)" "$(1)"
endef

# выемка артефакта из образа: $(1) — образ, $(2) — путь внутри, $(3) — куда
define image-copy
cid=$$(docker create $(1) /bin/true)
docker cp "$$cid:$(2)" "$(3)"
docker rm "$$cid" > /dev/null
endef

# скрипт загрузки моделей в образе base: $(1) — pip-пакет качалки, $(2) — каталог назначения,
# $(3) — аргументы fetch_models.py. Файлы отдаются владельцу хоста
define fetch-model
mkdir -p "$(2)"
docker run --rm $(PROXY_ENV)                                          \
    -v "$(2)":/out -v "$(SCRIPTS_DIR)":/scripts:ro                   \
    --entrypoint sh $(BASE_IMAGE) -c '
        set -e
        pip3 install --no-cache-dir "$(1)"
        python3 /scripts/fetch_models.py $(3)
        chown -R $(shell id -u):$(shell id -g) /out'
endef

# offline-цели не качают: недостающий вход — ошибка с подсказкой
define check-inputs
for path in $(INPUTS); do
    if [ ! -e "$$path" ]; then
        echo "нет $$path — запусти: make fetch" >&2
        exit 1
    fi
done
endef

# dev и release берут песочницу из src/sandbox, а не собирают её сами
define check-sandbox
for path in $(SANDBOX_PARTS); do
    if [ ! -e "$$path" ]; then
        echo "нет $$path — запусти: make sandbox" >&2
        exit 1
    fi
done
endef

define check-plugins
if [ ! -d "$(SANDBOX_DIR)/plugins" ]; then
    echo "нет $(SANDBOX_DIR)/plugins — запусти: make plugin-rootfs-all" >&2
    exit 1
fi
endef

define openapi
$(VENV_PYTHON) -m boba.studio.api.schema > "$(WEB_PROJECT)/openapi.json"
echo "== openapi: $(WEB_PROJECT)/openapi.json =="
endef

# --- справка --------------------------------------------------------------------

.PHONY: help
help:
	@echo "Сборка boba-studio $(VERSION)"
	echo ""
	echo "  make fetch     всё, что нужно из сети, -> $(DEPS_DIR): tarball'ы bwrap/fuse/e2fsprogs, uv, tessdata, веса fastembed, katex/Inter/mermaid"
	echo "  make sandbox   образы песочницы (offline): third/bin, workspace.ext4, rootfs.ext4"
	echo "  make build     образ $(IMAGE) (offline)"
	echo "  make test      проверка релиза внутри образа"
	echo "  make dev       рантайм в $(SRC_DIR)/compose/studio из src/ — для отладки и docker compose"
	echo "  make release   дерево установки из образа RELEASE_IMAGE -> $(OUT),"
	echo "                 ставится в INSTALL_DIR=$(INSTALL_DIR), данные — в DATA_DIR=$(DATA_DIR)"
	echo "  make web       страница workflow: npm ci/check/build в $(NODE_IMAGE) -> assets/workflow"
	echo "  make web-dev   vite dev-сервер страницы (127.0.0.1:$(WEB_DEV_PORT)); STOP=1 — остановить"
	echo "  make clean     удалить release и src/, кроме моделей"
	echo "  make all       fetch + sandbox + plugin-rootfs-all + web + build + dev: образ и дерево compose/studio"
	echo ""
	echo "  Версия одна на все пакеты: version — показать, version-set VER=X.Y.Z — проставить"
	echo ""
	echo "  Закрытый контур: conf/{pip.conf,uv.toml} — индексы, conf/ca-chain.crt — CA;"
	echo "  прокси из env уходит в docker; зеркала tarball'ов: UV_URL, BWRAP_URL, LIBFUSE_URL,"
	echo "  E2FSPROGS_URL, TESSDATA_REPO, KATEX_URL, INTER_URL, MERMAID_URL"
	echo ""
	echo "  Переменные: BOBA_TAG, NO_CACHE=1, PYTHON_VERSION=$(PYTHON_VERSION), GLIBC_IMAGE, GCC_IMAGE,"
	echo "              PYTHON_IMAGE, NODE_IMAGE, INSTALL_DIR, DATA_DIR, DEST_DIR; хосту нужен python >= 3.11"

# --- версия ---------------------------------------------------------------------

.PHONY: version version-set version-check
version:
	@echo $(VERSION)

version-set:
	@$(PYTHON) $(SCRIPTS_DIR)/version.py set $(SRC_DIR)/packages "$(VER)"

version-check:
	@$(PYTHON) $(SCRIPTS_DIR)/version.py check $(SRC_DIR)/packages

# --- fetch: единственная цель с сетью ------------------------------------------

.PHONY: fetch
fetch: $(INPUTS)
	@du -sh $(DEPS_DIR)/*

$(BWRAP_TARBALL):
	@$(call fetch,$(BWRAP_URL),$@)

$(LIBFUSE_TARBALL):
	@$(call fetch,$(LIBFUSE_URL),$@)

$(E2FSPROGS_TARBALL):
	@$(call fetch,$(E2FSPROGS_URL),$@)

$(UV_BIN):
	@$(call fetch,$(UV_URL),/tmp/uv-$(UV_VERSION).tar.gz)
	tar -xzf /tmp/uv-$(UV_VERSION).tar.gz -C /tmp
	install -D -m 0755 /tmp/uv-$(UV_PLATFORM)/uv "$@"
	rm -rf /tmp/uv-$(UV_PLATFORM) /tmp/uv-$(UV_VERSION).tar.gz
	"$@" --version

$(DEPS_DIR)/tessdata/%.traineddata:
	@$(call fetch,$(TESSDATA_REPO)/$*.traineddata,$@)

$(DEPS_DIR)/fastembed/.%: | base
	@$(call fetch-model,fastembed==$(FASTEMBED_VERSION),$(DEPS_DIR)/fastembed,fastembed "$(subst --,/,$*)" /out)
	touch "$@"

$(WEBASSETS_DIR)/katex/katex.min.css:
	@tmp=$$(mktemp -d)
	$(call fetch,$(KATEX_URL),$$tmp/katex.tar.gz)
	tar -xzf "$$tmp/katex.tar.gz" -C "$$tmp" --wildcards katex/katex.min.css 'katex/fonts/*.woff2'
	mkdir -p "$(@D)/fonts"
	cp "$$tmp/katex/katex.min.css" "$(@D)/"
	cp "$$tmp"/katex/fonts/*.woff2 "$(@D)/fonts/"
	rm -rf "$$tmp"

$(WEBASSETS_DIR)/inter/inter.css:
	@tmp=$$(mktemp -d)
	$(call fetch,$(INTER_URL),$$tmp/inter.tgz)
	tar -xzf "$$tmp/inter.tgz" -C "$$tmp"
	mkdir -p "$(@D)/files"
	: > "$@"
	for weight in $(INTER_WEIGHTS); do
	    cat "$$tmp/package/$$weight.css" >> "$@"
	    cp "$$tmp"/package/files/inter-*-$$weight-normal.woff2 "$(@D)/files/"
	done
	rm -rf "$$tmp"

$(WEBASSETS_DIR)/mermaid/mermaid.min.js:
	@tmp=$$(mktemp -d)
	$(call fetch,$(MERMAID_URL),$$tmp/mermaid.tgz)
	tar -xzf "$$tmp/mermaid.tgz" -C "$$tmp" package/dist/mermaid.min.js
	mkdir -p "$(@D)"
	cp "$$tmp/package/dist/mermaid.min.js" "$@"
	rm -rf "$$tmp"

# среда сборки и качалок моделей: gcc + python + индексы и CA закрытого контура
.PHONY: base
base: $(UV_BIN)
	@if [ "$$(printf '%s\n' "$(REQUIRES_PY)" "$(PYTHON_VERSION)" | sort -V | head -1)" != "$(REQUIRES_PY)" ]; then
	    echo "PYTHON_VERSION=$(PYTHON_VERSION), а пакетам нужен python >=$(REQUIRES_PY)" >&2
	    exit 1
	fi
	$(call docker-stage,base,$(BASE_IMAGE),--tag boba-base-$(APP):latest)

# --- offline: песочница, образы, релиз ------------------------------------------

# песочница приложения: bwrap и fuse2fs, шаблон workspace, образ корня с инструментами
.PHONY: sandbox
sandbox: version-check
	@$(check-inputs)
	echo "== песочница $(APP) =="
	$(call docker-stage,bwrap-build,$(BWRAP_IMAGE))
	$(call docker-stage,fuse2fs-build,$(FUSE_IMAGE))
	mkdir -p "$(SANDBOX_DIR)/third/bin"
	rm -f "$(SANDBOX_DIR)/third/bin/bwrap" "$(SANDBOX_DIR)/third/bin/fuse2fs" \
	      "$(SANDBOX_DIR)/workspace.ext4"
	$(call image-copy,$(BWRAP_IMAGE),/out/bwrap,$(SANDBOX_DIR)/third/bin/bwrap)
	$(call image-copy,$(FUSE_IMAGE),/out/fuse2fs,$(SANDBOX_DIR)/third/bin/fuse2fs)
	$(call image-copy,$(FUSE_IMAGE),/out/sandbox-workspace.ext4,$(SANDBOX_DIR)/workspace.ext4)
	chmod 0755 "$(SANDBOX_DIR)/third/bin/bwrap" "$(SANDBOX_DIR)/third/bin/fuse2fs"
	# TrustedBinaries отвергает каталог, открытый группе на запись
	chmod -R go-w "$(SANDBOX_DIR)/third"
	ls -lh $(SANDBOX_DIR)/third/bin $(SANDBOX_DIR)/workspace.ext4

# образ корня одного плагина: цель читает декларации [tool.boba.sandbox] пакета
.PHONY: plugin-rootfs
plugin-rootfs: version-check
	@$(check-inputs)
	@if [ -z "$(PLUGIN)" ]; then
	    echo "нужен PLUGIN=<имя пакета>: make plugin-rootfs PLUGIN=boba-tool-postgres" >&2
	    exit 1
	fi
	echo "== образ корня плагина $(PLUGIN) =="
	$(call docker-stage,plugin-rootfs,boba-plugin-$(PLUGIN):$(BOBA_TAG),$(APP_ARGS) --build-arg PLUGIN=$(PLUGIN))
	mkdir -p "$(SANDBOX_DIR)/plugins/$(PLUGIN)"
	rm -f "$(SANDBOX_DIR)/plugins/$(PLUGIN)/rootfs.ext4"
	$(call image-copy,boba-plugin-$(PLUGIN):$(BOBA_TAG),/out/plugin-rootfs.ext4,$(SANDBOX_DIR)/plugins/$(PLUGIN)/rootfs.ext4)
	ls -lh "$(SANDBOX_DIR)/plugins/$(PLUGIN)/rootfs.ext4"

# образы всех плагинов с entry points группы boba.tools
.PHONY: plugin-rootfs-all
plugin-rootfs-all:
	@for name in $$(python3 "$(BASE_DIR)/scripts/plugin_rootfs.py" list "$(SRC_DIR)/packages"); do
	    $(MAKE) -f $(MAKEFILE) plugin-rootfs PLUGIN=$$name || exit 1
	done


.PHONY: build
build: version-check
	@$(check-inputs)
	echo "== образ $(IMAGE), релиз внутри него лежит в $(DEST_DIR) =="
	$(call docker-stage,runtime-studio,$(IMAGE),--tag boba-$(APP):latest $(APP_ARGS))

# проверка релиза внутри образа тем же окружением, что уедет в compose и systemd
.PHONY: test
test: build
	@$(check-sandbox)
	$(check-plugins)
	echo "== проверка релиза $(VERSION) в образе $(IMAGE) =="
	env_file="$(DEPS_DIR)/boba-$(APP).env"
	export BOBA_BASE="$(DEST_DIR)" BOBA_VERSION="$(VERSION)"
	envsubst '$${BOBA_BASE} $${BOBA_VERSION}' < "$(CONF_DIR)/boba.env" > "$$env_file"
	# модели и песочница в образе не лежат, в релизе они рядом: подставляем их биндами
	docker run --rm -i --env-file "$$env_file" --entrypoint sh                              \
	    -v "$(DEPS_DIR)/fastembed:$(DEST_DIR)/models/fastembed:ro"                            \
	    -v "$(DEPS_DIR)/tessdata:$(DEST_DIR)/models/tessdata:ro"                              \
	    -v "$(SANDBOX_DIR)/plugins:$(DEST_DIR)/sandbox/plugins:ro"                            \
	    -v "$(SANDBOX_DIR)/workspace.ext4:$(DEST_DIR)/sandbox/workspace.ext4:ro"              \
	    $(IMAGE)                                                                              \
	    -c '"$$PYTHONHOME/bin/python3" - --names "$(DEST_DIR)/app/packages.txt"' < "$(TEST_DIR)/bobacheck.py"

# что обязано лежать в дереве установки
RELEASE_PARTS_common := sandbox/plugins sandbox/workspace.ext4 third/bin/bwrap third/bin/fuse2fs \
                        app/bin app_root/public
RELEASE_PARTS := $(RELEASE_PARTS_common) app_root/public/workflow models/fastembed models/tessdata

# дерево установки из готового образа: пути и шебанги переводятся с DEST_DIR на INSTALL_DIR
.PHONY: release
release:
	@if ! docker image inspect $(RELEASE_IMAGE) > /dev/null 2>&1; then
	    echo "нет образа $(RELEASE_IMAGE) — собери: make build" >&2
	    exit 1
	fi
	$(check-inputs)
	$(check-sandbox)
	$(check-plugins)
	rm -rf "$(OUT)"
	mkdir -p "$(OUT)/conf"
	$(call image-copy,$(RELEASE_IMAGE),$(DEST_DIR)/.,$(OUT)/)
	mkdir -p "$(OUT)/sandbox" "$(OUT)/models"
	cp -a "$(SANDBOX_DIR)/plugins" "$(OUT)/sandbox/plugins"
	cp -a "$(SANDBOX_DIR)/workspace.ext4" "$(OUT)/sandbox/workspace.ext4"
	cp -al "$(DEPS_DIR)/fastembed" "$(OUT)/models/fastembed"
	cp -al "$(DEPS_DIR)/tessdata" "$(OUT)/models/tessdata"
	sed -i "1s|^#!.*python3.*|#!$(INSTALL_DIR)/third/bin/python3|" "$(OUT)/app/bin"/*
	export BOBA_BASE="$(INSTALL_DIR)" BOBA_VERSION="$(VERSION)" INSTALL_DIR="$(INSTALL_DIR)" BOBA_DATA="$(DATA_DIR)"
	envsubst '$${INSTALL_DIR} $${BOBA_DATA}' < "$(CONF_DIR)/boba-$(APP).service" > "$(OUT)/boba-$(APP).service"
	envsubst '$${BOBA_BASE} $${BOBA_VERSION} $${BOBA_DATA}' < "$(CONF_DIR)/boba.env" > "$(OUT)/conf/boba.env"
	install -m 0755 "$(CONF_DIR)/cgroup-init.sh" "$(OUT)/cgroup-init.sh"
	chmod go-w "$(OUT)"
	chmod -R go-w "$(OUT)/third" "$(OUT)/sandbox"
	for path in $(RELEASE_PARTS); do
	    if [ ! -e "$(OUT)/$$path" ]; then
	        echo "в дереве нет $$path — собери заново: make build release" >&2
	        exit 1
	    fi
	done
	echo ">>> релиз готов: $(OUT) ($$(du -sh --apparent-size "$(OUT)" | cut -f1))"
	echo "    conf/config.toml в дереве нет — положи его при установке"
	echo "    установка: cp -a $(OUT)/. $(INSTALL_DIR)/"

# рабочее дерево приложения для отладки и docker compose: раскладка релиза, но из
# src/, без образа; docker-compose*.yml лежат в нём же и трекаются git'ом
.PHONY: dev
dev: OUT = $(SRC_DIR)/compose/$(APP)
dev:
	@$(check-inputs)
	$(check-sandbox)
	if [ ! -d "$(WEB_DIR)/assets/workflow" ]; then
	    echo "нет $(WEB_DIR)/assets/workflow — запусти: make web" >&2
	    exit 1
	fi
	echo "== рантайм $(APP) в $(OUT) =="
	mkdir -p "$(OUT)/conf" "$(OUT)/data/workspace" "$(OUT)/data/tool-logs" "$(OUT)/data/dump" "$(OUT)/data/krb"
	# в dev веса эмбеддера нужны process-режиму отладки; релиз их не таскает
	rm -rf "$(OUT)/models"
	mkdir -p "$(OUT)/models"
	cp -al "$(DEPS_DIR)/fastembed" "$(OUT)/models/fastembed"
	cp -al "$(DEPS_DIR)/tessdata" "$(OUT)/models/tessdata"
	install -m 0755 "$(CONF_DIR)/cgroup-init.sh" "$(OUT)/cgroup-init.sh"
	# dev-хост работает process-режимом: образы песочницы живут только в релизе,
	# но bwrap и fuse2fs нужны хранилищу приложения (workspace.ext4 канваса)
	rm -rf "$(OUT)/sandbox" "$(OUT)/third" "$(OUT)/app_root"
	mkdir -p "$(OUT)/third" "$(OUT)/app_root/public"
	cp -al "$(SANDBOX_DIR)/third/bin" "$(OUT)/third/bin"
	chmod -R go-w "$(OUT)/third"
	cp -a "$(WEB_DIR)/assets/workflow" "$(OUT)/app_root/public/workflow"
	cp -a "$(WEBASSETS_DIR)" "$(OUT)/app_root/public/vendor"
	export BOBA_BASE="$(DEST_DIR)" BOBA_VERSION="$(VERSION)" BOBA_DATA="$(DEST_DIR)/data"
	envsubst '$${BOBA_BASE} $${BOBA_VERSION} $${BOBA_DATA}' < "$(CONF_DIR)/boba.env" > "$(OUT)/conf/boba.env"
	chmod go-w "$(OUT)"
	if [ ! -f "$(OUT)/conf/config.toml" ]; then
	    echo "нет $(OUT)/conf/config.toml — положи конфиг приложения $(APP)" >&2
	    exit 1
	fi
	echo ">>> дерево готово: $(OUT)"
	echo "    debug:   python -m boba.studio --config $(OUT)/conf/config.toml"
	echo "    compose: cd $(OUT) && docker compose up -d"

# --- страница workflow ----------------------------------------------------------

.PHONY: web web-dev
web:
	@$(openapi)
	echo "== workflow page: $(NODE_IMAGE) =="
	docker run --rm --entrypoint /bin/sh -u "$$(id -u):$$(id -g)" -e HOME=/tmp \
	    -v "$(WEB_DIR):/pkg" -w /pkg/web/workflow $(NODE_IMAGE) \
	    -c 'npm ci --no-audit --no-fund && npm run generate && npm run check && npm run build'
	du -sh $(WEB_DIR)/assets/workflow
	echo "    в дерево studio сборку кладёт: make dev APP=studio"

# vite с HMR в контейнере; приложение с BOBA_WORKFLOW_PAGE=http://127.0.0.1:$(WEB_DEV_PORT)
# проксирует его под {BOBA_URL_PREFIX}/workflow-dev. Живой контейнер не трогается
ifeq ($(STOP),1)
web-dev:
	docker rm -f $(WEB_DEV_NAME) > /dev/null 2>&1 || true
else
web-dev:
	@$(openapi)
	running=$$(docker ps --filter name=^$(WEB_DEV_NAME)$$ --format '{{.Label "boba.url_prefix"}}')
	if [ "$$running" = "$(BOBA_URL_PREFIX)" ] && curl -sf -m 3 -o /dev/null "$(WEB_DEV_URL)"; then
	    echo "  ✓ $(WEB_DEV_NAME) is running: $(WEB_DEV_URL)"
	    exit 0
	fi
	docker rm -f $(WEB_DEV_NAME) > /dev/null 2>&1 || true
	docker run -d --name $(WEB_DEV_NAME) --entrypoint /bin/sh \
	    --label boba.url_prefix=$(BOBA_URL_PREFIX) \
	    -u "$$(id -u):$$(id -g)" -e HOME=/tmp -e BOBA_URL_PREFIX=$(BOBA_URL_PREFIX) \
	    -p 127.0.0.1:$(WEB_DEV_PORT):5173 \
	    -v "$(WEB_DIR):/pkg" -w /pkg/web/workflow $(NODE_IMAGE) \
	    -c '[ -d node_modules ] || npm ci --no-audit --no-fund; npm run generate; exec npm run dev -- --host 0.0.0.0 --port 5173 --strictPort --base $(BOBA_URL_PREFIX)/workflow-dev/'
	for i in $$(seq $(WEB_DEV_WAIT)); do
	    if curl -sf -m 3 -o /dev/null "$(WEB_DEV_URL)"; then
	        echo "== $(WEB_DEV_NAME): vite is ready at $(WEB_DEV_URL), logs: docker logs -f $(WEB_DEV_NAME) =="
	        exit 0
	    fi
	    if [ -z "$$(docker ps -q --filter name=^$(WEB_DEV_NAME)$$)" ]; then
	        echo "== $(WEB_DEV_NAME) exited; last log lines: =="
	        docker logs --tail 30 $(WEB_DEV_NAME)
	        exit 1
	    fi
	    sleep 1
	done
	echo "== $(WEB_DEV_NAME): vite did not answer at $(WEB_DEV_URL) in $(WEB_DEV_WAIT)s =="
	docker logs --tail 30 $(WEB_DEV_NAME)
	exit 1
endif

# --- прочее ---------------------------------------------------------------------

.PHONY: sec clean all
sec:
	@"$(SRC_DIR)/sec.sh" $(MODE)

# веса эмбеддера не трогаем: качаются долго
clean:
	rm -rf $(RELEASE_DIR) $(DEPS_DIR)/uv $(DEPS_DIR)/tessdata $(WEBASSETS_DIR) $(SANDBOX_DIR) \
	       $(DEPS_DIR)/*.tar.xz $(DEPS_DIR)/*.env

all: fetch sandbox plugin-rootfs-all web build dev
