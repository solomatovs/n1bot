# от каталога Makefile, а не от cwd: make -f build/Makefile из корня искал бы
# test/ и conf/ рядом с собой
BASE_DIR        ?= $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
SRC_DIR         ?= $(abspath $(BASE_DIR)/..)
# вложенные вызовы: make -f build/Makefile не подставляет его в $(MAKE)
MAKEFILE        := $(BASE_DIR)/Makefile
BUILD_CONTEXT   ?= $(SRC_DIR)
TEST_DIR        ?= $(BASE_DIR)/test
# шаблоны для сборки (make release, Dockerfile COPY) живут в build/conf
CONF_DIR        := $(BASE_DIR)/conf

# артефакты для сборки: tarball'ы, uv, tessdata, fastembed, webassets, wheels, sandbox
DEPS_DIR        ?= $(BASE_DIR)/src
# результаты make release — в корне репо, каталогом с версией
RELEASE_DIR     := $(SRC_DIR)/release


.DEFAULT_GOAL := help

# рецепт целиком в одном shell: без -e упавшая команда в середине не свалит цель
.ONESHELL:
SHELL           := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c


# версия у всех пакетов одна, лежит в их pyproject.toml (менять: make version-set)
VERSION_FILE    := $(SRC_DIR)/packages/core/boba-toolkit/pyproject.toml
VERSION         := $(shell sed -n 's/^version = "\(.*\)"/\1/p' $(VERSION_FILE))
BOBA_TAG        ?= $(VERSION)
REQUIRES_PY     := $(shell sed -n 's/^requires-python = ">=\([0-9]*\.[0-9]*\).*/\1/p' $(VERSION_FILE))

# корень установки на сервере: в него уедет дерево из make release
INSTALL_DIR     ?= /app/boba
# данные, переживающие релизы: workspace пользователей, дампы, журналы.
# Лежат рядом с деревом версии, а не внутри — иначе терялись бы при обновлении
DATA_DIR        ?= /app/boba/data
# тот же корень внутри образа
DEST_DIR        ?= /app/boba

# финальный runtime-образ
IMAGE           := boba:$(BOBA_TAG)
# из какого образа make release достаёт дерево установки и куда его кладёт
RELEASE_IMAGE   ?= $(IMAGE)
OUT             ?= $(RELEASE_DIR)/$(VERSION)

# режим sec.sh: all, sast, sca или rules
MODE            ?= all

PYTHON_VERSION  ?= 3.11
# чистая рантайм-база (только glibc), от неё строится финальный образ runtime
GLIBC_IMAGE     ?= dmp/glibc:2.28
# среда сборки: glibc 2.28 + gcc + python-дистрибутив
GCC_IMAGE       ?= dmp/gcc:8.5.0
PYTHON_IMAGE    ?= dmp/python:$(PYTHON_VERSION)-dist
# корень песочницы одним ext4-файлом: стадия sandbox-image
ROOTFS_EXT4_IMAGE := boba-sandbox-image:$(PYTHON_VERSION)
# шаблон workspace и fuse2fs: стадия fuse2fs-build
FUSE_IMAGE      := boba-fuse2fs:$(PYTHON_VERSION)
# bwrap: стадия bwrap-build
BWRAP_IMAGE     := boba-bwrap:$(PYTHON_VERSION)
# артефакты песочницы: rootfs, python, site и данные подключаются профилями
SANDBOX_DIR     := $(DEPS_DIR)/sandbox
# нативное окружение liteparse: office/растр/pdf он конвертирует этими
# программами. Убрать libreoffice — doc перестанет читать docx/xlsx/pptx
LITEPARSE_PACKAGES?= imagemagick libreoffice-writer libreoffice-calc libreoffice-impress ghostscript fonts-dejavu-core
# тег с версией python: иначе deps соберутся на boba-base от прошлой сборки
BASE_IMAGE      := boba-base:$(PYTHON_VERSION)

# OCR-модели tesseract
TESS_LANGS      ?= eng rus osd
TESSDATA_REPO   ?= https://github.com/tesseract-ocr/tessdata_best/raw/main
# embedding-веса fastembed; версия качалки фиксируется — иначе в образ
# приедет произвольная сборка со своим набором моделей
FASTEMBED_VERSION ?= 0.8.0
EMBED_MODELS    ?= intfloat/multilingual-e5-large

# модели переформулировщика: onnxruntime-genai держит каждую каталогом
# genai_config.json + веса + токенайзер, а формат ответа задаёт грамматика.
# Запись: репозиторий:подкаталог:имя каталога, пустой подкаталог — корень.
# Какую из скачанных берёт профиль, решает model_dir в config.toml
ONNX_MODELS     ?= xiaoyao9184/Qwen3-0.6B-onnx-genai:cpu_and_mobile/cpu-int4-rtn-block-32-acc-level-4/:qwen3-0.6b-int4 \
    Hug0007/qwen3-1.7b-int4-onnx-genai::qwen3-1.7b-int4                                                               \
    robinsmits/qwen3-4b-instruct-2507-onnx-int4::qwen3-4b-int4
ONNX_HUB_URL    ?= https://huggingface.co

# фронт chainlit тянет шрифт и стили формул с CDN; в закрытом контуре их
# кладут в data/public/vendor, а ссылки в index.html переписывает Dockerfile
WEBASSETS_DIR   := $(DEPS_DIR)/webassets
# ассеты фронта в дереве исходников: отсюда их берут и образ, и make dev
ASSETS_DIR      := $(SRC_DIR)/packages/agents/boba-chainlit/assets
ASSETS_VENDOR   := $(ASSETS_DIR)/public/vendor
KATEX_VERSION   ?= 0.16.22
KATEX_URL       ?= https://github.com/KaTeX/KaTeX/releases/download/v$(KATEX_VERSION)/katex.tar.gz
INTER_VERSION   ?= 5.3.0
INTER_URL       ?= https://registry.npmjs.org/@fontsource/inter/-/inter-$(INTER_VERSION).tgz
# начертания Inter, которые запрашивает фронт
INTER_WEIGHTS   ?= 400 500 700
MERMAID_VERSION ?= 11.16.1
MERMAID_URL     ?= https://registry.npmjs.org/mermaid/-/mermaid-$(MERMAID_VERSION).tgz

# страница workflow собирается node'ом образа: на хосте node не нужен
NODE_IMAGE      ?= dmp/nodejs:22
WEB_DIR         := $(SRC_DIR)/packages/agents/boba-studio

# готовый бинарь; для внутреннего mirror переопредели UV_URL
UV_VERSION      ?= 0.11.21
UV_PLATFORM     ?= x86_64-unknown-linux-musl
UV_URL          ?= https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)/uv-$(UV_PLATFORM).tar.gz

# bubblewrap: собирается стадией bwrap-build
BWRAP_VERSION   ?= 0.11.2
BWRAP_URL       ?= https://github.com/containers/bubblewrap/releases/download/v$(BWRAP_VERSION)/bubblewrap-$(BWRAP_VERSION).tar.xz
BWRAP_TARBALL   := $(DEPS_DIR)/bubblewrap-$(BWRAP_VERSION).tar.xz

# fuse2fs + libfuse: собираются стадией fuse2fs-build.
# 3.12.0 — последний релиз, которому хватает meson 0.49 из apt Astra 2.12
# (3.13+ требуют >= 0.50); релизы до 3.15 включительно выложены как .tar.xz
LIBFUSE_VERSION ?= 3.12.0
LIBFUSE_URL     ?= https://github.com/libfuse/libfuse/releases/download/fuse-$(LIBFUSE_VERSION)/fuse-$(LIBFUSE_VERSION).tar.xz
LIBFUSE_TARBALL := $(DEPS_DIR)/fuse-$(LIBFUSE_VERSION).tar.xz
E2FSPROGS_VERSION?= 1.47.2
E2FSPROGS_URL   ?= https://www.kernel.org/pub/linux/kernel/people/tytso/e2fsprogs/v$(E2FSPROGS_VERSION)/e2fsprogs-$(E2FSPROGS_VERSION).tar.xz
E2FSPROGS_TARBALL:= $(DEPS_DIR)/e2fsprogs-$(E2FSPROGS_VERSION).tar.xz
# размер шаблонного workspace-образа: лимит диска на тред
SANDBOX_WS_SIZE ?= 1G

# сбросить ли кэш сборки: docker build --no-cache
NO_CACHE        ?= 0
NO_CACHE_OPT    := $(if $(filter 1,$(NO_CACHE)),--no-cache,)

# --progress есть только у buildkit; на старом docker флага нет — не подставляем
PROGRESS        ?= $(shell docker build --help 2>/dev/null | grep -q -- '--progress' && echo '--progress plain')

# закрытый контур: CA внутреннего TLS (nexus). Кладётся в conf/ рядом с pip.conf и uv.toml
CA_CHAIN        := $(firstword $(wildcard $(CONF_DIR)/ca-chain.crt) $(wildcard $(BASE_DIR)/ca-chain.crt))
# curl на хосте: прокси берёт из env, CA — отсюда
CURL_OPTS       := $(if $(CA_CHAIN),--cacert "$(CA_CHAIN)")

# nexus в docker run: каталогом, поштучный bind-mount ломается о /etc/pip.conf,
# который стадия base уже создала. CA берётся из conf/, как и в Dockerfile
CONF_OPTS       := -v "$(CONF_DIR)":/tmp/conf:ro                                             \
    $(if $(wildcard $(CONF_DIR)/pip.conf),-e PIP_CONFIG_FILE=/tmp/conf/pip.conf)             \
    $(if $(wildcard $(CONF_DIR)/uv.toml),-e UV_CONFIG_FILE=/tmp/conf/uv.toml)                \
    $(if $(wildcard $(CONF_DIR)/ca-chain.crt),-e REQUESTS_CA_BUNDLE=/tmp/conf/ca-chain.crt   \
    -e CURL_CA_BUNDLE=/tmp/conf/ca-chain.crt)

# прокси в docker build: apt/pip/uv внутри стадий ходят в nexus через него
PROXY_VARS      := http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY
PROXY_ARGS      := $(foreach v,$(PROXY_VARS),$(if $($(v)),--build-arg $(v)="$($(v))"))

# сборка одной стадии: $(1) — стадия, $(2) — тег, $(3) — дополнительные аргументы
define docker-stage
docker build -f $(BASE_DIR)/Dockerfile $(NO_CACHE_OPT)   \
    $(DOCKER_ARGS) $(PROXY_ARGS) $(3)                    \
    --target $(1)                                        \
    --tag $(2)                                           \
    $(PROGRESS) $(BUILD_CONTEXT)
endef

# загрузка: $(1) — url, $(2) — файл
define fetch
curl -fSL $(CURL_OPTS) -o "$(2)" "$(1)"
endef

# выемка артефакта из образа: $(1) — образ, $(2) — путь внутри, $(3) — куда
define image-copy
cid=$$(docker create $(1) /bin/true)
docker cp "$$cid:$(2)" "$(3)"
docker rm "$$cid" > /dev/null
endef

# общие build-arg: образы тулчейна и версии
DOCKER_ARGS     = --build-arg GLIBC_IMAGE=$(GLIBC_IMAGE)   \
    --build-arg GCC_IMAGE=$(GCC_IMAGE)                     \
    --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE)               \
    --build-arg PYTHON_VERSION=$(PYTHON_VERSION)           \
    --build-arg BWRAP_VERSION=$(BWRAP_VERSION)             \
    --build-arg LIBFUSE_VERSION=$(LIBFUSE_VERSION)         \
    --build-arg E2FSPROGS_VERSION=$(E2FSPROGS_VERSION)     \
    --build-arg SANDBOX_WS_SIZE=$(SANDBOX_WS_SIZE)         \
    --build-arg SANDBOX_DEV_KRB=$(SRC_DIR)/compose/conf/krb   \
    --build-arg LITEPARSE_PACKAGES="$(LITEPARSE_PACKAGES)"


.PHONY: help
help:
	@echo "Сборка boba $(VERSION) из дерева пакетов $(SRC_DIR) -> $(IMAGE)"
	echo ""
	echo "  make sources   исходники, собираемые внутри Dockerfile -> $(DEPS_DIR)"
	echo "  make web       страница workflow: npm ci/check/build в $(NODE_IMAGE) -> assets/workflow"
	echo "  make web-dev   vite dev-сервер страницы в контейнере $(WEB_DEV_NAME) (127.0.0.1:$(WEB_DEV_PORT));"
	echo "                 приложение с BOBA_WORKFLOW_PAGE=http://127.0.0.1:$(WEB_DEV_PORT) проксирует его,"
	echo "                 бандл не нужен; make web-dev-stop — остановить"
	echo "  make deps      бинарные зависимости -> $(DEPS_DIR)/"
	echo "                 {uv,tessdata,fastembed,onnx-genai,webassets,"
	echo "                  sandbox/{rootfs.ext4,workspace.ext4},third/bin}"
	echo "                 цели по отдельности: uv, tessdata, fastembed, onnx-genai,"
	echo "                 webassets, sandbox-bin, sandbox-image, sandbox-workspace"
	echo "  make build     собрать образ $(IMAGE)"
	echo "  make test      проверить релиз внутри образа"
	echo "  make dev       рантайм в $(SRC_DIR)/compose: то же дерево из build/src,"
	echo "                 без образа — им пользуются и отладка, и docker compose"
	echo "  make release   дерево установки из готового образа:"
	echo "                 RELEASE_IMAGE=$(RELEASE_IMAGE), OUT=$(OUT)"
	echo "                 {conf,app_root,app,sandbox,third,models},"
	echo "                 ставится в INSTALL_DIR=$(INSTALL_DIR), данные — в DATA_DIR=$(DATA_DIR)"
	echo "  make sec       ruff, bandit, semgrep и pip-audit (MODE=all|sast|sca|rules)"
	echo "                 MODE=full — охват и правила как у сканера ИБ, без порогов"
	echo "  make clean     удалить исходники, deps и собранную папку (кроме fastembed)"
	echo ""
	echo "  make all       sources + deps + build + test"
	echo ""
	echo "  Версия (одна на все пакеты, лежит в pyproject.toml):"
	echo "    version        показать"
	echo "    version-set    VER=X.Y.Z — проставить во все пакеты и во внутренние пины"
	echo "    version-check  проверить, что версия одна и пины boba-* совпадают"
	echo ""
	echo "  Закрытый контур (offline + nexus):"
	echo "    conf/pip.conf, conf/uv.toml  индексы пакетов (примеры рядом)"
	echo "    conf/ca-chain.crt            CA внутреннего TLS: ставится в bundle"
	echo "                                 образа и подставляется в curl хоста"
	echo "    http_proxy/https_proxy/no_proxy из env уходят в docker build"
	echo "    Зеркала tarball'ов (иначе github/kernel.org/npm):"
	echo "      UV_URL, BWRAP_URL, LIBFUSE_URL, E2FSPROGS_URL, TESSDATA_REPO,"
	echo "      KATEX_URL, INTER_URL, MERMAID_URL, ONNX_HUB_URL"
	echo ""
	echo "  Переменные: INSTALL_DIR=$(INSTALL_DIR), DEST_DIR=$(DEST_DIR),"
	echo "              BOBA_TAG=<тег>, NO_CACHE=1,"
	echo "              PYTHON_VERSION=$(PYTHON_VERSION), GLIBC_IMAGE=$(GLIBC_IMAGE),"
	echo "              GCC_IMAGE=$(GCC_IMAGE), PYTHON_IMAGE=$(PYTHON_IMAGE)"
	echo "  Пакетам нужен python >=$(REQUIRES_PY)"


# значения, по которым отлаживают сборку: печатаются при вызове цели
DEBUG_VARS      := BASE_DIR SRC_DIR CONF_DIR DEPS_DIR RELEASE_DIR TEST_DIR   \
    VERSION IMAGE RELEASE_IMAGE OUT DEST_DIR INSTALL_DIR                     \
    BASE_IMAGE GLIBC_IMAGE GCC_IMAGE PYTHON_IMAGE REQUIRES_PY NO_CACHE       \
    UV_URL CA_CHAIN

.PHONY: debug
debug:
	@$(foreach v,$(DEBUG_VARS),printf '%-14s = %s\n' "$(v)" "$($(v))";)


# Все pyproject.toml пакетов; packages/<pkg>/build/ — артефакты сборки, не пакеты.
PYPROJECTS      = $(shell find $(SRC_DIR)/packages -name pyproject.toml -not -path '*/build/*')


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
	sed -i -e 's/^version = "$(VERSION)"$$/version = "$(VER)"/'   \
	       -e 's/"\(boba-[a-z0-9-]*\(\[[a-z0-9,_-]*\]\)\?\)==$(VERSION)"/"\1==$(VER)"/g' $(PYPROJECTS)
	echo ">>> $(VERSION) -> $(VER) в $(words $(PYPROJECTS)) пакетах"
	$(MAKE) -f $(MAKEFILE) --no-print-directory version-check VERSION=$(VER)


# ловит ручную правку pyproject.toml: разные version или пин boba-* на чужую версию
.PHONY: version-check
version-check:
	@bad=$$(grep -h '^version = ' $(PYPROJECTS) | sort -u | grep -v '^version = "$(VERSION)"$$' || true)
	if [ -n "$$bad" ]; then
	    echo "версии пакетов разошлись с $(VERSION):" >&2
	    echo "$$bad" >&2
	    exit 1
	fi
	bad=$$(grep -hoE '^[[:space:]]+"boba-[a-z0-9-]+[^"]*"' $(PYPROJECTS) | tr -d '[:blank:]'   \
	       | grep -v '^"boba-[a-z0-9-]*\(\[[a-z0-9,_-]*\]\)\?==$(VERSION)"$$' | sort -u || true)
	if [ -n "$$bad" ]; then
	    echo "внутренние зависимости не запинены на $(VERSION):" >&2
	    echo "$$bad" >&2
	    exit 1
	fi
	echo "version-check: $(VERSION), $(words $(PYPROJECTS)) пакетов — ok"


.PHONY: sources
sources:
	@mkdir -p $(DEPS_DIR)
	echo "== bubblewrap $(BWRAP_VERSION) =="
	if [ ! -f "$(BWRAP_TARBALL)" ]; then
	    $(call fetch,$(BWRAP_URL),$(BWRAP_TARBALL))
	fi
	echo "== libfuse $(LIBFUSE_VERSION) =="
	if [ ! -f "$(LIBFUSE_TARBALL)" ]; then
	    $(call fetch,$(LIBFUSE_URL),$(LIBFUSE_TARBALL))
	fi
	echo "== e2fsprogs $(E2FSPROGS_VERSION) =="
	if [ ! -f "$(E2FSPROGS_TARBALL)" ]; then
	    $(call fetch,$(E2FSPROGS_URL),$(E2FSPROGS_TARBALL))
	fi
	ls -lh $(DEPS_DIR)/


.PHONY: deps
deps: uv tessdata fastembed onnx-genai webassets sandbox-bin sandbox-workspace sandbox-image
	@du -sh $(DEPS_DIR)/uv $(WEBASSETS_DIR) $(SANDBOX_DIR)/*


.PHONY: uv
uv:
	@mkdir -p $(DEPS_DIR)/uv
	echo "== uv $(UV_VERSION) ($(UV_PLATFORM)) =="
	if [ -f "$(DEPS_DIR)/uv/uv" ]; then
	    echo "  ✓ $(DEPS_DIR)/uv/uv"
	else
	    $(call fetch,$(UV_URL),/tmp/uv-$(UV_VERSION).tar.gz)
	    tar -xzf /tmp/uv-$(UV_VERSION).tar.gz -C /tmp
	    install -m 0755 /tmp/uv-$(UV_PLATFORM)/uv $(DEPS_DIR)/uv/uv
	    rm -rf /tmp/uv-$(UV_PLATFORM) /tmp/uv-$(UV_VERSION).tar.gz
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
	        continue
	    fi

	    $(call fetch,$(TESSDATA_REPO)/$$lang.traineddata,$$dest)
	done


# dev-сервер страницы workflow: контейнер с vite и HMR, приложение проксирует
# его под {BOBA_URL_PREFIX}/workflow-dev; живой контейнер не трогается,
# неотвечающий пересоздаётся; цель ждёт готовности vite
WEB_DEV_NAME    ?= boba-web-dev
WEB_DEV_PORT    ?= 5173
BOBA_URL_PREFIX ?= /boba-debug
WEB_DEV_URL      = http://127.0.0.1:$(WEB_DEV_PORT)$(BOBA_URL_PREFIX)/workflow-dev/
WEB_DEV_WAIT    ?= 180
.PHONY: web-dev web-dev-stop
web-dev: openapi
	@running=$$(docker ps --filter name=^$(WEB_DEV_NAME)$$ --format '{{.Label "boba.url_prefix"}}')
	if [ "$$running" = "$(BOBA_URL_PREFIX)" ] && curl -sf -m 3 -o /dev/null "$(WEB_DEV_URL)"; then
	    echo "  ✓ $(WEB_DEV_NAME) is running: $(WEB_DEV_URL)"
	    exit 0
	fi
	docker rm -f $(WEB_DEV_NAME) >/dev/null 2>&1 || true
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
	        echo "== $(WEB_DEV_NAME) exited; last log lines: =="; docker logs --tail 30 $(WEB_DEV_NAME); exit 1
	    fi
	    sleep 1
	done
	echo "== $(WEB_DEV_NAME): vite did not answer at $(WEB_DEV_URL) in $(WEB_DEV_WAIT)s =="; docker logs --tail 30 $(WEB_DEV_NAME); exit 1

web-dev-stop:
	docker rm -f $(WEB_DEV_NAME) >/dev/null 2>&1 || true

# статика фронта: KaTeX (формулы), Inter (шрифт UI), mermaid (диаграммы) локально
# схема API для фронта: openapi.json из приложения без хоста, TS-типы генерирует npm run generate
VENV_PYTHON     ?= $(SRC_DIR)/.venv/bin/python
OPENAPI_JSON     = $(WEB_DIR)/web/workflow/openapi.json
.PHONY: openapi
openapi:
	$(VENV_PYTHON) -m boba.studio.api.schema > "$(OPENAPI_JSON)"
	@echo "== openapi: $(OPENAPI_JSON) =="

.PHONY: webassets
web: openapi
	@echo "== workflow page: $(NODE_IMAGE) =="
	docker run --rm --entrypoint /bin/sh -u "$$(id -u):$$(id -g)" -e HOME=/tmp \
	    -v "$(WEB_DIR):/pkg" -w /pkg/web/workflow $(NODE_IMAGE) \
	    -c 'npm ci --no-audit --no-fund && npm run generate && npm run check && npm run build'
	@du -sh $(WEB_DIR)/assets/workflow
	# dev app_root держит копию сборки ([studio] dist): обновляем её, если
	# dev-дерево уже собрано
	if [ -d "$(SRC_DIR)/compose/app_root/public" ]; then
	    rm -rf "$(SRC_DIR)/compose/app_root/public/workflow"
	    cp -a "$(WEB_DIR)/assets/workflow" "$(SRC_DIR)/compose/app_root/public/workflow"
	fi

webassets:
	@mkdir -p $(WEBASSETS_DIR)/katex/fonts $(WEBASSETS_DIR)/inter/files $(WEBASSETS_DIR)/mermaid
	echo "== katex $(KATEX_VERSION) =="
	if [ -f "$(WEBASSETS_DIR)/katex/katex.min.css" ]; then
	    echo "  ✓ $(WEBASSETS_DIR)/katex"
	else
	    tmp=$$(mktemp -d)
	    $(call fetch,$(KATEX_URL),$$tmp/katex.tar.gz)
	    # ttf и woff не нужны: woff2 стоит в src первым и его понимают все браузеры
	    tar -xzf "$$tmp/katex.tar.gz" -C "$$tmp" --wildcards   \
	        katex/katex.min.css 'katex/fonts/*.woff2'
	    cp "$$tmp/katex/katex.min.css" "$(WEBASSETS_DIR)/katex/"
	    cp "$$tmp"/katex/fonts/*.woff2 "$(WEBASSETS_DIR)/katex/fonts/"
	    rm -rf "$$tmp"
	fi
	echo "== Inter $(INTER_VERSION): $(INTER_WEIGHTS) =="
	if [ -f "$(WEBASSETS_DIR)/inter/inter.css" ]; then
	    echo "  ✓ $(WEBASSETS_DIR)/inter"
	else
	    tmp=$$(mktemp -d)
	    $(call fetch,$(INTER_URL),$$tmp/inter.tgz)
	    tar -xzf "$$tmp/inter.tgz" -C "$$tmp"
	    : > "$(WEBASSETS_DIR)/inter/inter.css"
	    # <вес>.css несёт все подсеты с unicode-range, включая кириллицу
	    for w in $(INTER_WEIGHTS); do
	        cat "$$tmp/package/$$w.css" >> "$(WEBASSETS_DIR)/inter/inter.css"
	        cp "$$tmp"/package/files/inter-*-$$w-normal.woff2 "$(WEBASSETS_DIR)/inter/files/"
	    done
	    rm -rf "$$tmp"
	fi
	echo "== mermaid $(MERMAID_VERSION) =="
	if [ -f "$(WEBASSETS_DIR)/mermaid/mermaid.min.js" ]; then
	    echo "  ✓ $(WEBASSETS_DIR)/mermaid"
	else
	    tmp=$$(mktemp -d)
	    $(call fetch,$(MERMAID_URL),$$tmp/mermaid.tgz)
	    # из пакета нужен один IIFE-бандл: он выставляет globalThis.mermaid
	    tar -xzf "$$tmp/mermaid.tgz" -C "$$tmp" package/dist/mermaid.min.js
	    cp "$$tmp/package/dist/mermaid.min.js" "$(WEBASSETS_DIR)/mermaid/"
	    rm -rf "$$tmp"
	fi
	# assets/public/theme.json просит /public/vendor/inter/inter.css — путь обязан
	# существовать и при запуске из IDE, где public берётся из дерева исходников.
	# В образ тот же каталог кладёт COPY build/src/webassets (стадия runtime)
	rm -rf "$(ASSETS_VENDOR)"
	mkdir -p "$(dir $(ASSETS_VENDOR))"
	cp -a "$(WEBASSETS_DIR)" "$(ASSETS_VENDOR)"
	du -sh $(WEBASSETS_DIR)/*


# bwrap и fuse2fs рядом с остальными артефактами: без них dev-дерево
# пришлось бы доставать из релизного образа
.PHONY: sandbox-bin
sandbox-bin:
	@dest="$(SANDBOX_DIR)/third/bin"
	echo "== bwrap и fuse2fs =="
	$(call docker-stage,bwrap-build,$(BWRAP_IMAGE))
	$(call docker-stage,fuse2fs-build,$(FUSE_IMAGE))
	mkdir -p "$$dest"
	$(call image-copy,$(BWRAP_IMAGE),/out/bwrap,$$dest/bwrap)
	$(call image-copy,$(FUSE_IMAGE),/out/fuse2fs,$$dest/fuse2fs)
	chmod 0755 "$$dest/bwrap" "$$dest/fuse2fs"
	"$$dest/bwrap" --version
	"$$dest/fuse2fs" -V


# состав песочницы объявляют сами пакеты: у кого есть extra payload, тот и едет
# внутрь вместе с ним (парсеры, эмбеддер, plotly)
.PHONY: sandbox-names
sandbox-names:
	@dest="$(DEPS_DIR)/names-sandbox.txt"
	echo "== пакеты песочницы =="
	mkdir -p "$(DEPS_DIR)"
	: > "$$dest"

	for project in $(PYPROJECTS); do
	    extras=$$(sed -n '/^\[project.optional-dependencies\]/,/^\[/p' "$$project")

	    if ! echo "$$extras" | grep -q '^payload'; then
	        continue
	    fi

	    name=$$(sed -n 's/^name = "\(.*\)"/\1/p' "$$project" | head -1)
	    echo "$$name[payload]" >> "$$dest"
	done

	sort -o "$$dest" "$$dest"
	sed 's/^/    /' "$$dest"


# корень песочницы одним ext4-файлом: python, site-packages, код инструментов
# и tessdata внутри, в рантайме монтируется read-only. Веса эмбеддера внутрь
# не идут: они приезжают биндом профиля kb из models/fastembed
.PHONY: sandbox-image
sandbox-image: version-check sandbox-names tessdata
	@dest="$(SANDBOX_DIR)/rootfs.ext4"
	echo "== образ корня песочницы =="
	$(call docker-stage,sandbox-image,$(ROOTFS_EXT4_IMAGE))
	mkdir -p "$(SANDBOX_DIR)"
	rm -f "$$dest"
	$(call image-copy,$(ROOTFS_EXT4_IMAGE),/out/sandbox-rootfs.ext4,$$dest)
	ls -lh "$$dest"


.PHONY: sandbox-workspace
sandbox-workspace:
	@dest="$(SANDBOX_DIR)/workspace.ext4"
	# шаблон делает стадия fuse2fs-build тем же mke2fs, что уедет в релиз:
	# второй способ создания означал бы два разных образа под одним именем
	echo "== шаблон workspace $(SANDBOX_WS_SIZE) =="
	$(call docker-stage,fuse2fs-build,$(FUSE_IMAGE))
	mkdir -p "$(SANDBOX_DIR)"
	$(call image-copy,$(FUSE_IMAGE),/out/sandbox-workspace.ext4,$$dest)
	ls -lh "$$dest"


.PHONY: fastembed
fastembed:
	@mkdir -p $(DEPS_DIR)/fastembed
	echo "== веса fastembed: $(EMBED_MODELS) =="

	# каталог весов на месте — ни образа сборки, ни pip, ни сети
	for dir in $(DEPS_DIR)/fastembed/models--*; do
	    if [ -d "$$dir" ]; then
	        echo "  ✓ $$dir"
	        exit 0
	    fi
	done

	$(MAKE) -f $(MAKEFILE) --no-print-directory base
	docker run --rm                                                                       \
	  -e HOST_UID="$$(id -u)" -e HOST_GID="$$(id -g)"                                     \
	  -e EMBED_MODELS="$(EMBED_MODELS)"                                                    \
	  -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY -e http_proxy -e https_proxy -e no_proxy   \
	  $(CONF_OPTS)                                                                        \
	  -v "$(DEPS_DIR)/fastembed":/fastembed                                               \
	  --entrypoint sh $(BASE_IMAGE) -c '
	    set -e
	    pip3 install --no-cache-dir "fastembed==$(FASTEMBED_VERSION)"

	    for model in $$EMBED_MODELS; do
	      echo ">>> fastembed: $$model"
	      MODEL="$$model" python3 -c "
	import os
	from fastembed import TextEmbedding
	embed = TextEmbedding(model_name=os.environ[\"MODEL\"], cache_dir=\"/fastembed\")
	list(embed.embed([\"probe\"]))
	"
	    done

	    chown -R "$$HOST_UID:$$HOST_GID" /fastembed'


# модели переформулировщика: качаются один раз и уезжают в образ приложения,
# а не в песочницу — генерация идёт в процессе chainlit
.PHONY: onnx-genai
onnx-genai:
	@mkdir -p $(DEPS_DIR)/onnx-genai
	echo "== модели переформулировщика =="

	# скачиваем только те, каталога которых нет
	missing=""
	for spec in $(ONNX_MODELS); do
	    name=$$(echo "$$spec" | cut -d: -f3)

	    if [ -d "$(DEPS_DIR)/onnx-genai/$$name" ]; then
	        echo "  ✓ $(DEPS_DIR)/onnx-genai/$$name"
	        continue
	    fi

	    missing="$$missing $$spec"
	done

	if [ -z "$$missing" ]; then
	    exit 0
	fi

	$(MAKE) -f $(MAKEFILE) --no-print-directory base
	docker run --rm                                                                       \
	  -e HOST_UID="$$(id -u)" -e HOST_GID="$$(id -g)"                                     \
	  -e HF_ENDPOINT="$(ONNX_HUB_URL)"                                                    \
	  -e ONNX_MODELS="$$missing"                                                          \
	  -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY -e http_proxy -e https_proxy -e no_proxy   \
	  $(CONF_OPTS)                                                                        \
	  -v "$(DEPS_DIR)/onnx-genai":/onnx                                                   \
	  --entrypoint sh $(BASE_IMAGE) -c '
	    set -e
	    pip3 install --no-cache-dir huggingface_hub

	    for spec in $$ONNX_MODELS; do
	      REPO=$$(echo "$$spec" | cut -d: -f1)
	      SUBDIR=$$(echo "$$spec" | cut -d: -f2)
	      NAME=$$(echo "$$spec" | cut -d: -f3)
	      echo ">>> onnx-genai: $$REPO"
	      export REPO SUBDIR NAME
	      python3 -c "
	import os, pathlib, shutil
	from huggingface_hub import snapshot_download
	subdir = os.environ[\"SUBDIR\"]
	root = snapshot_download(os.environ[\"REPO\"], allow_patterns=[subdir + \"*\"])
	dest = pathlib.Path(\"/onnx\") / os.environ[\"NAME\"]
	shutil.rmtree(dest, ignore_errors=True)
	shutil.copytree(pathlib.Path(root) / subdir, dest)
	print(dest)
	"
	    done

	    chown -R "$$HOST_UID:$$HOST_GID" /onnx'


.PHONY: base
base: uv
	@if [ "$$(printf '%s\n' "$(REQUIRES_PY)" "$(PYTHON_VERSION)" | sort -V | head -1)" != "$(REQUIRES_PY)" ]; then
	    echo "PYTHON_VERSION=$(PYTHON_VERSION), а пакетам нужен python >=$(REQUIRES_PY)" >&2
	    exit 1
	fi
	$(call docker-stage,base,$(BASE_IMAGE),--tag boba-base:latest)


# что должно лежать в build/src до сборки образа
BUILD_INPUTS    :=                          \
    $(BWRAP_TARBALL)                        \
    $(LIBFUSE_TARBALL)                      \
    $(E2FSPROGS_TARBALL)                    \
    $(DEPS_DIR)/uv/uv                       \
    $(DEPS_DIR)/tessdata/eng.traineddata    \
    $(DEPS_DIR)/fastembed                   \
    $(DEPS_DIR)/onnx-genai                  \
    $(WEBASSETS_DIR)/katex/katex.min.css    \
    $(WEBASSETS_DIR)/inter/inter.css        \
    $(DEPS_DIR)/names-sandbox.txt

.PHONY: check-artifacts
check-artifacts:
	@for path in $(BUILD_INPUTS); do
	    if [ ! -e "$$path" ]; then
	        echo "нет $$path — запусти: make sources deps" >&2
	        exit 1
	    fi
	done


.PHONY: build
build: version-check check-artifacts
	@echo ">>> образ $(IMAGE), релиз внутри него лежит в $(DEST_DIR)"
	$(call docker-stage,runtime,$(IMAGE),--tag boba:latest   \
	    --build-arg BOBA_DIR="$(DEST_DIR)" --build-arg BOBA_VERSION="$(VERSION)")


.PHONY: test
test: build
	@[ -f "$(TEST_DIR)/bobacheck.py" ] || {
	    echo "нет $(TEST_DIR)/bobacheck.py — файл лежит в репозитории, TEST_DIR=$(TEST_DIR)" >&2
	    exit 1
	}
	echo "== проверка релиза $(VERSION) в образе $(IMAGE) =="
	# boba.env в образе нет: окружение даёт тот, кто запускает процесс.
	# тест берёт тот же шаблон, что уедет в compose и systemd
	env_file=$$(mktemp)
	trap 'rm -f "$$env_file"' EXIT
	export BOBA_BASE="$(DEST_DIR)" BOBA_VERSION="$(VERSION)"
	envsubst '$${BOBA_BASE} $${BOBA_VERSION}' < "$(CONF_DIR)/boba.env"   \
	    | grep -v '^$$' > "$$env_file"

	docker run --rm -i --env-file "$$env_file"   \
	  --entrypoint sh $(IMAGE) -c '
	    "$$PYTHONHOME/bin/python3" - --names "$(DEST_DIR)/app/packages.txt"
	  ' < "$(TEST_DIR)/bobacheck.py"

# что обязано лежать в дереве установки
RELEASE_PARTS   :=              \
    sandbox/rootfs.ext4         \
    sandbox/workspace.ext4      \
    third/bin/bwrap             \
    third/bin/fuse2fs           \
    models/onnx-genai           \
    models/fastembed            \
    app/bin                     \
    app_root/public

.PHONY: release
release:
	@docker image inspect $(RELEASE_IMAGE) > /dev/null 2>&1 || {
	    echo "нет образа $(RELEASE_IMAGE) — собери: make build" >&2
	    exit 1
	}

	rm -rf "$(OUT)"
	mkdir -p "$(OUT)"
	cid=$$(docker create --entrypoint /bin/sh $(RELEASE_IMAGE))
	trap 'docker rm -f "$$cid" > /dev/null 2>&1 || true' EXIT
	docker cp "$$cid:$(DEST_DIR)/." "$(OUT)/"
	docker rm "$$cid" > /dev/null
	trap - EXIT

	$(MAKE) -f $(MAKEFILE) --no-print-directory install-tree OUT="$(OUT)" BASE="$(INSTALL_DIR)"

	echo ">>> релиз готов: $(OUT)  ($$(du -sh --apparent-size "$(OUT)" 2>/dev/null | cut -f1))"
	echo "    приложение:     $$(du -sh --apparent-size "$(OUT)/app" 2>/dev/null | cut -f1)"
	echo "    песочница:      $$(du -sh --apparent-size "$(OUT)/sandbox" 2>/dev/null | cut -f1)"
	echo "    установка:      cp -a $(OUT)/. $(INSTALL_DIR)/"


# рабочее дерево для отладки и docker compose: та же раскладка, что у релиза,
# но собирается из build/src, а не из образа — образ для отладки не нужен
.PHONY: dev
dev: check-artifacts
	@out="$(SRC_DIR)/compose"
	echo "== рантайм в $$out =="
	mkdir -p "$$out/conf" "$$out/data/workspace" "$$out/data/tool-logs" "$$out/data/dump"

	# песочница двумя образами: корень и шаблон workspace, жёсткими ссылками
	rm -rf "$$out/sandbox"
	mkdir -p "$$out/sandbox"
	cp -al "$(SANDBOX_DIR)/rootfs.ext4" "$$out/sandbox/rootfs.ext4"
	cp -al "$(SANDBOX_DIR)/workspace.ext4" "$$out/sandbox/workspace.ext4"

	mkdir -p "$$out/third"
	rm -rf "$$out/third/bin"
	cp -al "$(SANDBOX_DIR)/third/bin" "$$out/third/bin"

	# локальные модели чата и переформулировщика плюс веса эмбеддера: та же
	# раскладка, что в образе, веса едут в песочницу биндом профиля kb
	rm -rf "$$out/models/onnx-genai" "$$out/models/fastembed"
	mkdir -p "$$out/models"
	cp -al "$(DEPS_DIR)/onnx-genai" "$$out/models/onnx-genai"
	cp -al "$(DEPS_DIR)/fastembed" "$$out/models/fastembed"

	# код инструментов в деве приезжает биндом поверх /usr/src образа:
	# правка видна сразу, пересборка образа не нужна
	$(MAKE) -f $(MAKEFILE) --no-print-directory dev-pth OUT="$$out"

	# ассеты фронта: тот же набор, что уходит в образ
	rm -rf "$$out/app_root"
	mkdir -p "$$out/app_root/.chainlit" "$$out/app_root/.files"
	cp -a "$(ASSETS_DIR)/.chainlit/config.toml" "$$out/app_root/.chainlit/"
	cp -a "$(ASSETS_DIR)/.chainlit/translations" "$$out/app_root/.chainlit/"
	cp -a "$(ASSETS_DIR)/chainlit.md" "$$out/app_root/"
	cp -a "$(ASSETS_DIR)/public" "$$out/app_root/public"

	# окружение процесса в контейнере: пути внутри него, а не на хосте
	export BOBA_BASE="$(DEST_DIR)" BOBA_VERSION="$(VERSION)"
	export BOBA_DATA="$(DEST_DIR)/data"
	envsubst '$${BOBA_BASE} $${BOBA_VERSION} $${BOBA_DATA}'   \
	    < "$(CONF_DIR)/boba.env" > "$$out/conf/boba.env"
	sed -i 's|^BOBA_CGROUP_BASE=.*|BOBA_CGROUP_BASE=/cgroup/boba-sandbox|' "$$out/conf/boba.env"

	install -m 0755 "$(CONF_DIR)/cgroup-init.sh" "$$out/cgroup-init.sh"

	if [ ! -f "$$out/conf/config.toml" ]; then
	    echo "нет $$out/conf/config.toml — положи конфиг приложения" >&2
	    exit 1
	fi

	# umask каталога сборки даёт g+w, а TrustedBinaries такой third/bin
	# не примет — то же самое делает install-tree для релиза
	chmod go-w "$$out"
	chmod -R go-w "$$out/third" "$$out/sandbox"

	echo ">>> дерево готово: $$out"
	echo "    debug:  BOBA_BASE=$$out BOBA_APP=$(SRC_DIR)/packages"
	echo "    compose: cd $$out && docker compose up -d"


# список путей исходников для sys.path песочницы; в релизе тот же файл
# содержит один /usr/src, потому что код там уже плоский
.PHONY: dev-pth
dev-pth:
	@pth="$(OUT)/sandbox/boba-src.pth"
	: > "$$pth"
	for project in $$(find "$(SRC_DIR)/packages" -name pyproject.toml -not -path '*/build/*'); do
	    src="$$(dirname "$$project")/src"

	    if [ ! -d "$$src" ]; then
	        continue
	    fi

	    echo "/usr/src/$${src#$(SRC_DIR)/packages/}" >> "$$pth"
	done
	echo "    boba-src.pth: $$(wc -l < "$$pth") путей"


# общая часть release и любой установки: права, шебанги, conf, проверки
.PHONY: install-tree
install-tree:
	@mkdir -p "$(OUT)/conf"

	# шебанги указывают внутрь образа: переставляем на путь установки
	sed -i "1s|^#!.*python3.*|#!$(BASE)/third/bin/python3|" "$(OUT)/app/bin"/*

	# отсюда берутся bwrap и fuse2fs: групповая или чужая запись на пути до
	# них — отказ TrustedBinaries, а umask каталога сборки даёт как раз g+w
	chmod go-w "$(OUT)"
	chmod -R go-w "$(OUT)/third" "$(OUT)/sandbox"

	# envsubst с явным списком: $${PYTHONHOME} и TOML-интерполяции не трогаются
	export BOBA_BASE="$(BASE)" BOBA_VERSION="$(VERSION)" INSTALL_DIR="$(BASE)"
	export BOBA_DATA="$(DATA_DIR)"
	envsubst '$${INSTALL_DIR} $${BOBA_DATA}'   \
	    < "$(CONF_DIR)/boba-chainlit.service" > "$(OUT)/boba-chainlit.service"
	envsubst '$${INSTALL_DIR} $${BOBA_DATA}'   \
	    < "$(CONF_DIR)/boba-studio.service" > "$(OUT)/boba-studio.service"
	envsubst '$${BOBA_BASE} $${BOBA_VERSION} $${BOBA_DATA}'   \
	    < "$(CONF_DIR)/boba.env" > "$(OUT)/conf/boba.env"

	install -m 0755 "$(CONF_DIR)/cgroup-init.sh" "$(OUT)/cgroup-init.sh"

	if [ ! -f "$(OUT)/conf/config.toml" ]; then
	    echo "    conf/config.toml в дереве нет — положи его при установке"
	fi

	$(MAKE) -f $(MAKEFILE) --no-print-directory tree-check OUT="$(OUT)"


# проверки готового дерева: без них приложение падало бы уже на сервере
.PHONY: tree-check
tree-check:
	@for path in $(RELEASE_PARTS); do
	    if [ ! -e "$(OUT)/$$path" ]; then
	        echo "в дереве нет $$path — собери заново: make build release" >&2
	        exit 1
	    fi
	done

	# то же, что проверит TrustedBinaries на старте приложения
	writable=$$(find "$(OUT)/third/bin" -maxdepth 1 -type f -perm /022 -print -quit)
	if [ -n "$$writable" ]; then
	    echo "third/bin доступен на запись посторонним ($$writable)" >&2
	    exit 1
	fi

	echo ">>> артефакты на месте"


# fastembed и wheels не трогаем: веса качаются долго, колеса — кэш
.PHONY: clean
clean:
	rm -rf $(RELEASE_DIR)                                                        \
	       $(DEPS_DIR)/uv $(DEPS_DIR)/tessdata $(WEBASSETS_DIR) $(SANDBOX_DIR)   \
	       $(BWRAP_TARBALL) $(LIBFUSE_TARBALL) $(E2FSPROGS_TARBALL)


# SAST и SCA: сканеры ставятся изолированно через uvx, правила semgrep лежат
# в conf/semgrep (обновляются sec.sh rules)
.PHONY: sec
sec:
	@"$(SRC_DIR)/sec.sh" $(MODE)


.PHONY: print-tag
print-tag:
	@echo $(BOBA_TAG)


.PHONY: all
all: sources deps build test
