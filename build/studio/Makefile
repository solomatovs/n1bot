# Сборка boba-studio. Из сети берутся две вещи: артефакты цели fetch (github,
# huggingface, kernel.org — в src/) и пакеты pypi/npm при сборке — из реестров образов
# (в закрытом контуре nexus: conf/pip.conf, conf/uv.toml, npmrc образа nodejs).
# Остальные цели работают из src/ и локальных образов.

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
# артефакты для сборки: tarball'ы, uv, модели OCR и эмбеддера, webassets, sandbox
DEPS_DIR        ?= $(BASE_DIR)/src
RELEASE_DIR     := $(SRC_DIR)/release

APP             := studio
APP_PACKAGES     = boba-studio $(PLUGIN_PACKAGES)
APP_IMPORTS     := boba.studio
APP_HEALTH      := /api/openapi.json
APP_CGROUP      := boba-sandbox-studio

.DEFAULT_GOAL := help

# рецепт целиком в одном shell: без -e упавшая команда в середине не свалит цель
.ONESHELL:
SHELL           := /bin/bash
.SHELLFLAGS     := -eu -o pipefail -c

# версия у всех пакетов одна, лежит в их pyproject.toml (менять: make version-set);
# скрипты сборки зовутся python'ом venv репозитория, системный может быть старым
PYTHON          ?= $(SRC_DIR)/.venv/bin/python
VERSION_PY      := $(PYTHON) $(SCRIPTS_DIR)/version.py
VERSION         := $(shell $(VERSION_PY) show $(SRC_DIR)/packages)
# плагины приложения — пакеты с entry points группы boba.tools; списка в extras нет
PLUGIN_PACKAGES := $(shell $(PYTHON) $(SCRIPTS_DIR)/plugin_rootfs.py list $(SRC_DIR)/packages)
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
SANDBOX_DIR     := $(DEPS_DIR)/sandbox
# сколько build cache оставляет prune
PRUNE_KEEP      ?= 20GB

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

RAPIDOCR_VERSION ?= 3.9.2
RAPIDOCR_LANGS  ?= eslav
RAPIDOCR_REPO   ?= https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v$(RAPIDOCR_VERSION)/onnx
RAPIDOCR_FILES  := $(DEPS_DIR)/rapidocr/ch_PP-OCRv5_det_mobile.onnx \
                   $(DEPS_DIR)/rapidocr/ch_ppocr_mobile_v2.0_cls_mobile.onnx \
                   $(RAPIDOCR_LANGS:%=$(DEPS_DIR)/rapidocr/%_PP-OCRv5_rec_mobile.onnx)

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
                   $(RAPIDOCR_FILES) $(FASTEMBED_STAMPS) $(WEB_FILES)

# --- фронт: страница workflow ------------------------------------------------------

WEB_DIR         := $(SRC_DIR)/packages/agents/boba-studio
ASSETS_DIR      := $(WEB_DIR)/assets
# страница workflow: в образ её собирает стадия web-build, в dev-дерево — цель web;
# типы api берутся из openapi.json, который печатает установленное приложение
WEB_PROJECT     := $(WEB_DIR)/web/workflow
WEB_DIST        := $(ASSETS_DIR)/workflow
WEB_SRCS        := $(shell find $(WEB_PROJECT)/src -type f -not -name schema.d.ts) \
                   $(wildcard $(WEB_PROJECT)/*.json $(WEB_PROJECT)/*.ts $(WEB_PROJECT)/*.js $(WEB_PROJECT)/index.html)
OPENAPI_JSON    := $(WEB_PROJECT)/openapi.json
# vite dev-сервер страницы: make web-dev
WEB_DEV_NAME    ?= boba-web-dev
WEB_DEV_PORT    ?= 5173
BOBA_URL_PREFIX ?= /boba-studio-debug
WEB_DEV_URL      = http://127.0.0.1:$(WEB_DEV_PORT)$(BOBA_URL_PREFIX)/workflow-dev/
WEB_DEV_WAIT    ?= 180

# --- что кладут sandbox и release ------------------------------------------------

SANDBOX_PARTS   := $(SANDBOX_DIR)/third/bin/bwrap $(SANDBOX_DIR)/third/bin/fuse2fs $(SANDBOX_DIR)/workspace.ext4
# mke2fs из сборки: им make делает workspace.ext4 и rootfs плагинов вне docker build
SANDBOX_TOOLS   := $(SANDBOX_DIR)/tools/mke2fs $(SANDBOX_DIR)/tools/mke2fs.conf

RELEASE_PARTS   := sandbox/plugins sandbox/workspace.ext4 third/bin/bwrap third/bin/fuse2fs \
                   app/bin app_root/public app_root/public/workflow app_root/public/vendor \
                   models/fastembed models/rapidocr

# --- docker ---------------------------------------------------------------------

NO_CACHE        ?= 0
NO_CACHE_OPT    :=
ifeq ($(NO_CACHE),1)
NO_CACHE_OPT    := --no-cache
endif

# --progress есть только у buildkit
PROGRESS        ?= $(shell docker build --help 2>/dev/null | grep -q -- '--progress' && echo '--progress plain')

# закрытый контур: CA внутреннего TLS для curl хоста; в образы его кладёт Dockerfile из conf/
CA_CHAIN        := $(wildcard $(CONF_DIR)/ca-chain.crt)
CURL_OPTS       := $(if $(CA_CHAIN),--cacert "$(CA_CHAIN)")

# прокси: в docker build аргументами, в docker run окружением
PROXY_VARS      := http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY
PROXY_ARGS      := $(foreach v,$(PROXY_VARS),$(if $($(v)),--build-arg $(v)="$($(v))"))
PROXY_ENV       := $(foreach v,$(PROXY_VARS),-e $(v))

DOCKER_ARGS      = --build-arg GLIBC_IMAGE=$(GLIBC_IMAGE)              \
    --build-arg GCC_IMAGE=$(GCC_IMAGE)                                 \
    --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE)                           \
    --build-arg PYTHON_VERSION=$(PYTHON_VERSION)                       \
    --build-arg BWRAP_VERSION=$(BWRAP_VERSION)                         \
    --build-arg LIBFUSE_VERSION=$(LIBFUSE_VERSION)                     \
    --build-arg E2FSPROGS_VERSION=$(E2FSPROGS_VERSION)                 \
    --build-arg NODE_IMAGE=$(NODE_IMAGE)

# аргументы стадий одного приложения
APP_ARGS         = --build-arg APP=$(APP)                           \
    --build-arg APP_PACKAGES="$(APP_PACKAGES)"                      \
    --build-arg APP_IMPORTS="$(APP_IMPORTS)"                        \
    --build-arg HEALTH_PATH="$(APP_HEALTH)"                         \
    --build-arg SANDBOX_DEV_KRB=$(SRC_DIR)/compose/$(APP)/conf/krb  \
    --build-arg BOBA_DIR="$(DEST_DIR)" --build-arg BOBA_VERSION="$(VERSION)"

# сборка стадии в образ: $(1) — стадия, $(2) — тег, $(3) — дополнительные аргументы
define docker-stage
docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) $(DOCKER_ARGS) $(PROXY_ARGS) $(3) \
    --target $(1) --tag $(2) $(PROGRESS) $(BUILD_CONTEXT)
endef

# выгрузка файлов стадии в каталог без образа: $(1) — стадия, $(2) — каталог, $(3) — аргументы
define docker-output
docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) $(DOCKER_ARGS) $(PROXY_ARGS) $(3) \
    --target $(1) --output type=local,dest=$(2) $(PROGRESS) $(BUILD_CONTEXT)
endef

# файловая система стадии tar'ом в stdout: $(1) — стадия, $(2) — аргументы
define docker-tar
docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT) $(DOCKER_ARGS) $(PROXY_ARGS) $(2) \
    --target $(1) --output type=tar,dest=- $(PROGRESS) $(BUILD_CONTEXT)
endef

# mke2fs из src/sandbox/tools в контейнере glibc: $(1) — скрипт, $(2) — каталог результата,
# $(3) — аргументы скрипта. root в контейнере нужен, чтобы сохранить владельцев из tar
define mke2fs-run
docker run --rm -i -e MKE2FS_CONFIG=/tools/mke2fs.conf -e PATH=/tools:/usr/sbin:/usr/bin:/sbin:/bin \
    -v "$(SANDBOX_DIR)/tools:/tools:ro" -v "$(SCRIPTS_DIR):/scripts:ro" -v "$(2):/out" \
    --entrypoint sh $(GLIBC_IMAGE) /scripts/$(1) $(3) "$$(id -u):$$(id -g)"
endef

# загрузка: $(1) — url, $(2) — файл
define fetch
mkdir -p "$(dir $(2))"
curl -fSL $(CURL_OPTS) -o "$(2)" "$(1)"
endef

# скрипт загрузки моделей в образе base: $(1) — pip-пакет качалки, $(2) — каталог назначения,
# $(3) — аргументы fetch_models.py. Файлы отдаются владельцу хоста
define fetch-model
mkdir -p "$(2)"
docker run --rm $(PROXY_ENV)                                        \
    -v "$(2)":/out -v "$(SCRIPTS_DIR)":/scripts:ro                  \
    --entrypoint sh $(BASE_IMAGE) -c '
        set -e
        pip3 install --no-cache-dir "$(1)"
        python3 /scripts/fetch_models.py $(3)
        chown -R $(shell id -u):$(shell id -g) /out'
endef

# дерево релиза из образа: $(1) — образ, $(2) — путь внутри, $(3) — куда
define image-copy
cid=$$(docker create $(1) /bin/true)
docker cp "$$cid:$(2)" "$(3)"
docker rm "$$cid" > /dev/null
endef

# наличие путей: $(1) — список, $(2) — подсказка, что сделать
define check
for path in $(1); do
    if [ ! -e "$$path" ]; then
        echo "нет $$path — $(2)" >&2
        exit 1
    fi
done
endef

# опустошить каталог, не пересоздавая: compose держит его bind-mount'ом по inode,
# после rm -rf контейнер до перезапуска видел бы пустоту
define refill
mkdir -p "$(1)"
find "$(1)" -mindepth 1 -delete
endef

# --- справка --------------------------------------------------------------------

define HELP
Сборка boba-studio $(VERSION)

Сеть (артефакты -> $(DEPS_DIR)):
  fetch                 tarball'ы bwrap/fuse/e2fsprogs, uv, модели rapidocr, веса fastembed, katex/Inter/mermaid

Сборка (сеть — только реестры pypi/npm; образ один — $(IMAGE), остальное файлами в src/):
  sandbox               bwrap, fuse2fs и шаблон workspace.ext4 -> src/sandbox
  plugin-rootfs         rootfs.ext4 одного плагина: PLUGIN=<пакет>
  plugin-rootfs-all     rootfs.ext4 всех плагинов с entry points boba.tools
  web                   страница workflow для dev-дерева -> assets/workflow
  web-dev               vite dev-сервер страницы на 127.0.0.1:$(WEB_DEV_PORT); STOP=1 — остановить
  build                 образ $(IMAGE)
  test                  проверка релиза внутри образа

Деревья:
  dev                   рантайм в $(SRC_DIR)/compose/$(APP) из src/ для отладки и compose; зовёт web
  release               дерево установки из образа RELEASE_IMAGE -> $(OUT);
                        ставится в INSTALL_DIR=$(INSTALL_DIR), данные в DATA_DIR=$(DATA_DIR)

Прочее:
  all                   fetch sandbox plugin-rootfs-all web build dev
  clean                 удалить release, src/ и фронт, кроме моделей
  prune                 снести образы прошлых версий и промежуточные теги, build cache до PRUNE_KEEP=$(PRUNE_KEEP)
  sec                   sec.sh, MODE=all|sast|sca|rules
  version               показать версию пакетов
  version-set           проставить версию: VER=X.Y.Z
  version-check         версии и пины boba-* совпадают

Закрытый контур: conf/pip.conf, conf/uv.toml — индексы pypi; npm — npmrc образа nodejs;
conf/ca-chain.crt — CA; прокси из env уходит в docker. Зеркала артефактов: UV_URL,
BWRAP_URL, LIBFUSE_URL, E2FSPROGS_URL, RAPIDOCR_REPO, KATEX_URL, INTER_URL, MERMAID_URL.

Переменные: BOBA_TAG, NO_CACHE=1, PYTHON_VERSION=$(PYTHON_VERSION), GLIBC_IMAGE, GCC_IMAGE,
PYTHON_IMAGE, NODE_IMAGE, INSTALL_DIR, DATA_DIR, DEST_DIR; хосту нужен python >= 3.11.
endef

.PHONY: help
help:
	@:$(info $(HELP))

# --- версия ---------------------------------------------------------------------

.PHONY: version version-set version-check
version:
	@echo $(VERSION)

version-set:
	@$(VERSION_PY) set $(SRC_DIR)/packages "$(VER)"

version-check:
	@$(VERSION_PY) check $(SRC_DIR)/packages

# --- fetch: артефакты из сети -----------------------------------------------------

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

# модели OCR: детектор и классификатор общие, распознаватель — на язык (boba.doc OcrModel)
$(DEPS_DIR)/rapidocr/ch_PP-OCRv5_det_mobile.onnx:
	@$(call fetch,$(RAPIDOCR_REPO)/PP-OCRv5/det/$(notdir $@),$@)

$(DEPS_DIR)/rapidocr/ch_ppocr_mobile_v2.0_cls_mobile.onnx:
	@$(call fetch,$(RAPIDOCR_REPO)/PP-OCRv4/cls/$(notdir $@),$@)

$(DEPS_DIR)/rapidocr/%_PP-OCRv5_rec_mobile.onnx:
	@$(call fetch,$(RAPIDOCR_REPO)/PP-OCRv5/rec/$(notdir $@),$@)

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
	@$(VERSION_PY) python-check $(SRC_DIR)/packages $(PYTHON_VERSION)
	$(call docker-stage,base,$(BASE_IMAGE),--tag boba-base-$(APP):latest)

# --- песочница и плагины ---------------------------------------------------------

# песочница приложения: bwrap и fuse2fs, шаблон workspace
.PHONY: sandbox
sandbox: version-check
	@$(call check,$(INPUTS),запусти: make fetch)
	echo "== песочница $(APP) =="
	mkdir -p "$(SANDBOX_DIR)"
	rm -f $(SANDBOX_PARTS) $(SANDBOX_TOOLS)
	$(call docker-output,sandbox-out,$(SANDBOX_DIR))
	chmod 0755 "$(SANDBOX_DIR)/third/bin/bwrap" "$(SANDBOX_DIR)/third/bin/fuse2fs" "$(SANDBOX_DIR)/tools/mke2fs"
	$(call mke2fs-run,mkworkspace.sh,$(SANDBOX_DIR),/out/workspace.ext4 $(SANDBOX_WS_SIZE)) < /dev/null
	# TrustedBinaries отвергает каталог, открытый группе на запись
	chmod -R go-w "$(SANDBOX_DIR)/third"
	ls -lh $(SANDBOX_DIR)/third/bin $(SANDBOX_DIR)/workspace.ext4

# образ корня одного плагина: цель читает декларации [tool.boba.sandbox] пакета
PLUGIN_ROOTFS    = $(SANDBOX_DIR)/plugins/$(PLUGIN)/rootfs.ext4

.PHONY: plugin-rootfs
plugin-rootfs: version-check
	@$(if $(PLUGIN),,$(error нужен PLUGIN=<имя пакета>: make plugin-rootfs PLUGIN=boba-tool-postgres))
	$(call check,$(INPUTS),запусти: make fetch)
	$(call check,$(SANDBOX_TOOLS),запусти: make sandbox)
	echo "== образ корня плагина $(PLUGIN) =="
	mkdir -p "$(dir $(PLUGIN_ROOTFS))"
	# корень и site-packages стримятся в mke2fs двумя tar'ами: ни ext4, ни копия site
	# не оседают в build cache; из стадии plugin-site tarsub вырезает только /opt/site и код
	{
	    $(call docker-tar,plugin-root,$(APP_ARGS) --build-arg PLUGIN=$(PLUGIN))
	    $(call docker-tar,plugin-site,$(APP_ARGS) --build-arg PLUGIN=$(PLUGIN)) \
	        | $(PYTHON) "$(SCRIPTS_DIR)/tarsub.py" \
	            opt/site=usr/local/lib/python$(PYTHON_VERSION)/site-packages opt/code-sandbox=usr/src
	} | $(call mke2fs-run,mkrootfs.sh,$(dir $(PLUGIN_ROOTFS)),/out/rootfs.ext4 $(PYTHON_VERSION) 16)

# образы всех плагинов с entry points группы boba.tools
.PHONY: plugin-rootfs-all
plugin-rootfs-all:
	@for name in $(PLUGIN_PACKAGES); do
	    $(MAKE) -f "$(MAKEFILE)" plugin-rootfs PLUGIN=$$name
	done

# --- фронт: цель web для dev-дерева, в образ то же собирает стадия web-build --------

.PHONY: web
web: $(WEB_DIST)/index.html

# контракт api печатает приложение из venv репозитория; страница генерирует по нему типы
$(OPENAPI_JSON): $(shell find $(SRC_DIR)/packages/agents/boba-studio/src -name "*.py")
	@$(PYTHON) -m boba.studio.api.schema > "$@"
	echo "== openapi: $@ =="

# страница workflow node'ом образа: на хосте node не нужен
$(WEB_DIST)/index.html: $(WEB_SRCS) $(OPENAPI_JSON)
	@echo "== workflow page: $(NODE_IMAGE) =="
	docker run --rm --entrypoint /bin/sh -u "$$(id -u):$$(id -g)" -e HOME=/tmp \
	    -v "$(WEB_DIR):/pkg" -w /pkg/web/workflow $(NODE_IMAGE) \
	    -c 'npm ci --no-audit --no-fund && npm run generate && npm run check && npm run build'
	touch "$@"
	du -sh "$(WEB_DIST)"

# vite с HMR в контейнере; приложение с BOBA_WORKFLOW_PAGE=http://127.0.0.1:$(WEB_DEV_PORT)
# проксирует его под {BOBA_URL_PREFIX}/workflow-dev. Живой контейнер не трогается
.PHONY: web-dev
ifeq ($(STOP),1)
web-dev:
	docker rm -f $(WEB_DEV_NAME) > /dev/null 2>&1 || true
else
web-dev: $(OPENAPI_JSON)
	@running=$$(docker ps --filter name=^$(WEB_DEV_NAME)$$ --format '{{.Label "boba.url_prefix"}}')
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

# --- образ и его проверка ----------------------------------------------------------

.PHONY: build
build: version-check
	@$(call check,$(INPUTS),запусти: make fetch)
	echo "== образ $(IMAGE), релиз внутри него лежит в $(DEST_DIR) =="
	$(call docker-stage,runtime-studio,$(IMAGE),--tag boba-$(APP):latest $(APP_ARGS))

# проверка релиза внутри образа тем же окружением, что уедет в compose и systemd
.PHONY: test
test: build
	@$(call check,$(SANDBOX_PARTS),запусти: make sandbox)
	$(call check,$(SANDBOX_DIR)/plugins,запусти: make plugin-rootfs-all)
	echo "== проверка релиза $(VERSION) в образе $(IMAGE) =="
	# окружение контейнера — шаблон conf/boba.env с путями образа, живёт только на время проверки
	env_file=$$(mktemp)
	trap 'rm -f "$$env_file"' EXIT
	export BOBA_BASE="$(DEST_DIR)" BOBA_VERSION="$(VERSION)"
	envsubst '$${BOBA_BASE} $${BOBA_VERSION}' < "$(CONF_DIR)/boba.env" > "$$env_file"
	# модели и песочница в образе не лежат, в релизе они рядом: подставляем их биндами
	docker run --rm -i --env-file "$$env_file" --entrypoint sh                              \
	    -v "$(DEPS_DIR)/fastembed:$(DEST_DIR)/models/fastembed:ro"                            \
	    -v "$(DEPS_DIR)/rapidocr:$(DEST_DIR)/models/rapidocr:ro"                              \
	    -v "$(SANDBOX_DIR)/plugins:$(DEST_DIR)/sandbox/plugins:ro"                            \
	    -v "$(SANDBOX_DIR)/workspace.ext4:$(DEST_DIR)/sandbox/workspace.ext4:ro"              \
	    $(IMAGE)                                                                              \
	    -c '"$$PYTHONHOME/bin/python3" - --names "$(DEST_DIR)/app/packages.txt"' < "$(TEST_DIR)/bobacheck.py"

# --- деревья: release из образа, dev из src/ ---------------------------------------

# дерево установки из готового образа: пути и шебанги переводятся с DEST_DIR на INSTALL_DIR
.PHONY: release
release:
	@docker image inspect "$(RELEASE_IMAGE)" > /dev/null
	$(call check,$(INPUTS),запусти: make fetch)
	$(call check,$(SANDBOX_PARTS),запусти: make sandbox)
	$(call check,$(SANDBOX_DIR)/plugins,запусти: make plugin-rootfs-all)
	rm -rf "$(OUT)"
	mkdir -p "$(OUT)/conf" "$(OUT)/sandbox" "$(OUT)/models"
	$(call image-copy,$(RELEASE_IMAGE),$(DEST_DIR)/.,$(OUT)/)
	cp -a "$(SANDBOX_DIR)/plugins" "$(OUT)/sandbox/plugins"
	cp -a "$(SANDBOX_DIR)/workspace.ext4" "$(OUT)/sandbox/workspace.ext4"
	cp -al "$(DEPS_DIR)/fastembed" "$(OUT)/models/fastembed"
	cp -al "$(DEPS_DIR)/rapidocr" "$(OUT)/models/rapidocr"
	sed -i "1s|^#!.*python3.*|#!$(INSTALL_DIR)/third/bin/python3|" "$(OUT)/app/bin"/*
	export BOBA_BASE="$(INSTALL_DIR)" BOBA_VERSION="$(VERSION)" INSTALL_DIR="$(INSTALL_DIR)" BOBA_DATA="$(DATA_DIR)"
	envsubst '$${INSTALL_DIR} $${BOBA_DATA}' < "$(CONF_DIR)/boba-$(APP).service" > "$(OUT)/boba-$(APP).service"
	envsubst '$${BOBA_BASE} $${BOBA_VERSION} $${BOBA_DATA}' < "$(CONF_DIR)/boba.env" > "$(OUT)/conf/boba.env"
	install -m 0755 "$(CONF_DIR)/cgroup-init.sh" "$(OUT)/cgroup-init.sh"
	chmod go-w "$(OUT)"
	chmod -R go-w "$(OUT)/third" "$(OUT)/sandbox"
	$(call check,$(addprefix $(OUT)/,$(RELEASE_PARTS)),собери заново: make build release)
	echo ">>> релиз готов: $(OUT) ($$(du -sh --apparent-size "$(OUT)" | cut -f1))"
	echo "    conf/config.toml в дереве нет — положи его при установке"
	echo "    установка: cp -a $(OUT)/. $(INSTALL_DIR)/"

# рабочее дерево приложения для отладки и docker compose: раскладка релиза, но из
# src/, без образа; docker-compose*.yml лежат в нём же и трекаются git'ом
.PHONY: dev
dev: OUT = $(SRC_DIR)/compose/$(APP)
dev: web
	@$(call check,$(INPUTS),запусти: make fetch)
	$(call check,$(SANDBOX_PARTS),запусти: make sandbox)
	echo "== рантайм $(APP) в $(OUT) =="
	mkdir -p "$(OUT)/conf" "$(OUT)/data/workspace" "$(OUT)/data/tool-logs" "$(OUT)/data/dump" "$(OUT)/data/krb"
	# в dev веса эмбеддера нужны process-режиму отладки; релиз их не таскает
	$(call refill,$(OUT)/models)
	cp -al "$(DEPS_DIR)/fastembed" "$(OUT)/models/fastembed"
	cp -al "$(DEPS_DIR)/rapidocr" "$(OUT)/models/rapidocr"
	install -m 0755 "$(CONF_DIR)/cgroup-init.sh" "$(OUT)/cgroup-init.sh"
	# dev-хост работает process-режимом: образы песочницы живут только в релизе,
	# но bwrap и fuse2fs нужны хранилищу приложения (workspace.ext4 канваса)
	rm -rf "$(OUT)/sandbox"
	$(call refill,$(OUT)/third)
	cp -al "$(SANDBOX_DIR)/third/bin" "$(OUT)/third/bin"
	chmod -R go-w "$(OUT)/third"
	$(call refill,$(OUT)/app_root)
	mkdir -p "$(OUT)/app_root/public"
	cp -a "$(WEB_DIST)" "$(OUT)/app_root/public/workflow"
	cp -a "$(WEBASSETS_DIR)" "$(OUT)/app_root/public/vendor"
	export BOBA_BASE="$(DEST_DIR)" BOBA_VERSION="$(VERSION)" BOBA_DATA="$(DEST_DIR)/data"
	envsubst '$${BOBA_BASE} $${BOBA_VERSION} $${BOBA_DATA}' < "$(CONF_DIR)/boba.env" > "$(OUT)/conf/boba.env"
	chmod go-w "$(OUT)"
	$(call check,$(OUT)/conf/config.toml,положи конфиг приложения $(APP))
	echo ">>> дерево готово: $(OUT)"
	echo "    debug:   python -m boba.studio --config $(OUT)/conf/config.toml"
	echo "    compose: cd $(OUT) && docker compose up -d"

# --- прочее ---------------------------------------------------------------------

# всё целиком: артефакты, песочница, фронт, образ и compose-дерево. Compose монтирует
# app_root поверх образа, поэтому фронт нужен и в дереве, а не только в образе
.PHONY: all
all: fetch sandbox plugin-rootfs-all web build dev

.PHONY: sec
sec:
	@"$(SRC_DIR)/sec.sh" $(MODE)

# образы прошлых сборок и промежуточные теги старых схем, затем build cache до PRUNE_KEEP
.PHONY: prune
prune:
	@docker images --format '{{.Repository}}:{{.Tag}}' \
	    | { grep -E '^boba-(plugin|bwrap|fuse2fs|sandbox)' || true; } | xargs -r docker rmi
	docker images --format '{{.Repository}}:{{.Tag}}' \
	    | { grep -E '^boba-$(APP):' || true; } \
	    | { grep -v -E ':($(BOBA_TAG)|latest)$$' || true; } \
	    | { xargs -r -n1 docker rmi || true; }   # образ под живым контейнером остаётся
	docker builder prune -f --keep-storage $(PRUNE_KEEP)
	docker system df

# модели не трогаем: качаются долго
.PHONY: clean
clean:
	rm -rf $(RELEASE_DIR) $(DEPS_DIR)/uv $(DEPS_DIR)/rapidocr $(WEBASSETS_DIR) $(SANDBOX_DIR) \
	       $(DEPS_DIR)/*.tar.xz $(WEB_DIST) $(OPENAPI_JSON)
