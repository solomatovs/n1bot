BASE_DIR      	?= $(abspath .)
SRC_DIR       	?= $(abspath ..)
ARTIFACTS     	?= $(BASE_DIR)/artifacts
BUILD_CONTEXT 	?= $(SRC_DIR)
TEST_DIR		?= $(BASE_DIR)/test
DEPLOY_DIR		:= $(BASE_DIR)/conf


.DEFAULT_GOAL := help

# рецепт целиком в одном shell: без -e упавшая команда в середине не свалит цель
.ONESHELL:
SHELL       := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c


# версия пакетов и тег образа — одно и то же, меняется через make version-set.
# Переопределить разово: make build BOBA_TAG=<тег>

# версия у всех пакетов одна, лежит в их pyproject.toml (менять: make version-set)
VERSION_FILE := $(SRC_DIR)/packages/core/boba-patterns/pyproject.toml
VERSION      := $(shell sed -n 's/^version = "\(.*\)"/\1/p' $(VERSION_FILE))
BOBA_TAG     ?= $(VERSION)

# портативный релиз: собирается в STAGE_DIR (make build), на сервере лежит в DEST_DIR.
# BOBA_DIR (путь релиза) зашивается в conf/boba.env, conf/boba-chainlit.service и
# conf/config.toml через envsubst (шаблоны в build/conf/) — переносить релиз в
# другое место: пересобрать make build DEST_DIR=<путь>.
RELEASE_NAME := boba-$(VERSION)
STAGE_DIR    ?= $(BASE_DIR)/release/$(RELEASE_NAME)
DEST_DIR     ?= /app/boba/$(VERSION)


PYTHON_VERSION	?= 3.11
# чистая рантайм-база (только glibc), от неё строится финальный образ runtime
GLIBC_IMAGE  	?= dmp/glibc:2.28
# среда сборки: glibc 2.28 + gcc + python-дистрибутив (для wheels)
GCC_IMAGE    	?= dmp/gcc:8.5.0
PYTHON_IMAGE 	?= dmp/python:$(PYTHON_VERSION)-dist
# тег с версией python: иначе wheels соберутся на boba-base от прошлой сборки
BASE_IMAGE    := boba-base:$(PYTHON_VERSION)
REQUIRES_PY   := $(shell sed -n 's/^requires-python = ">=\([0-9]*\.[0-9]*\).*/\1/p' $(VERSION_FILE))
NO_CACHE		?= 0

# OCR-модели tesseract (скачиваются в make sources)
TESS_LANGS    	?= eng rus osd
TESSDATA_REPO 	?= https://github.com/tesseract-ocr/tessdata_best/raw/main
# embedding-веса fastembed (скачиваются в make deps)
EMBED_MODELS  	?= intfloat/multilingual-e5-large

# готовый бинарь; для внутреннего mirror переопредели UV_URL
UV_VERSION  	?= 0.11.21
UV_PLATFORM 	?= x86_64-unknown-linux-musl
UV_URL      	?= https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)/uv-$(UV_PLATFORM).tar.gz

# финальный runtime-образ (из него же достаётся каталог релиза)
IMAGE			:= boba:$(BOBA_TAG)
NO_CACHE_TEXT	:= $(if $(filter 1,$(NO_CACHE)),--no-cache,)

# закрытый контур: подхватываем ca-chain.crt, если положили в artifacts
CA_CHAIN := $(wildcard $(ARTIFACTS)/ca-chain.crt)


.PHONY: help
help:
	@echo "boba build — портативный релиз $(RELEASE_NAME), версия пакетов $(VERSION)"
	echo ""
	echo "Online-стадии (нужна сеть), порядок:"
	echo "  sources  скачать tessdata и бинарь uv, собрать boba-base (apt-пакеты)"
	echo "  deps     собрать offline-wheelhouse и скачать веса fastembed"
	echo ""
	echo "Offline-стадии (сеть не нужна, работают на артефактах выше):"
	echo "  build    собрать $(IMAGE) (финальный runtime-образ) и достать из него"
	echo "           портативный каталог в $(STAGE_DIR)"
	echo "  export   скопировать собранный каталог в $(DEST_DIR)"
	echo "  test     проверить релиз в контейнере (с системными библиотеками)"
	echo ""
	echo "Всё сразу: make all  (= sources deps build test)"
	echo ""
	echo "Версия (одна на все пакеты, лежит в pyproject.toml):"
	echo "  version        показать"
	echo "  version-set    VER=X.Y.Z — проставить во все пакеты и во внутренние пины"
	echo "  version-check  проверить, что версия одна и пины boba-* совпадают"
	echo ""
	echo "Прочее:"
	echo "  dist       собрать wheel'ы пакетов на хосте в build/dist (без docker)"
	echo "  clean      удалить скачанное в sources и собранный релиз (wheels и fastembed остаются)"
	echo "  print-tag  показать тег образа"
	echo ""
	echo "Переменные: DEST_DIR=$(DEST_DIR), BOBA_TAG=<тег>, NO_CACHE=1,"
	echo "            PYTHON_VERSION=$(PYTHON_VERSION), GLIBC_IMAGE=$(GLIBC_IMAGE),"
	echo "            GCC_IMAGE=$(GCC_IMAGE), PYTHON_IMAGE=$(PYTHON_IMAGE)"
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
	echo "TEST_DIR       = $(TEST_DIR)"
	echo "STAGE_DIR      = $(STAGE_DIR)"
	echo "DEST_DIR       = $(DEST_DIR)"
	echo "RELEASE_NAME   = $(RELEASE_NAME)"
	echo "BOBA_TAG       = $(BOBA_TAG)"
	echo "VERSION        = $(VERSION)"
	echo "IMAGE          = $(IMAGE)"
	echo "BASE_IMAGE     = $(BASE_IMAGE)"
	echo "GLIBC_IMAGE   = $(GLIBC_IMAGE)"
	echo "GCC_IMAGE      = $(GCC_IMAGE)"
	echo "PYTHON_IMAGE   = $(PYTHON_IMAGE)"
	echo "REQUIRES_PY    = $(REQUIRES_PY)"
	echo "NO_CACHE       = $(NO_CACHE)"
	echo "UV_URL         = $(UV_URL)"
	echo "CA_CHAIN       = $(CA_CHAIN)"


# ---------------------------------------------------------------------------
# Online-стадии: скачивание исходников/зависимостей.
# ---------------------------------------------------------------------------

.PHONY: sources
sources: base tessdata uv

.PHONY: deps
deps: wheels fastembed

# Все online-скачивания, что есть в make sources, врозь:
.PHONY: tessdata uv
tessdata:
	@mkdir -p $(ARTIFACTS)/tessdata
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

uv:
	@mkdir -p $(ARTIFACTS)/uv
	echo ">>> uv ($(UV_PLATFORM))"
	if [ -f "$(ARTIFACTS)/uv/uv" ]; then
	    echo "  ✓ $(ARTIFACTS)/uv/uv"
	else
	    echo ">>> $(UV_URL)"
	    curl -fSL "$(UV_URL)" | tar -xz -C /tmp
	    install -m 0755 /tmp/uv-$(UV_PLATFORM)/uv $(ARTIFACTS)/uv/uv
	    rm -rf /tmp/uv-$(UV_PLATFORM)
	    $(ARTIFACTS)/uv/uv --version
	fi

# среда сборки: gcc + python + uv + внешние программы (apt — нужна сеть).
# Эта же стадия — и основа runtime-образа, и контейнер для сборки каталога.
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
	    echo "нет образа $(BASE_IMAGE) — собери: make sources" >&2
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


# веса запекает в образ/релиз make build, поэтому запускать до него
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


# ---------------------------------------------------------------------------
# Offline-стадии: артефакты выше уже скачаны, сеть не нужна.
# ---------------------------------------------------------------------------

# проверяет, что всё необходимое для offline-сборки на месте
.PHONY: check-artifacts
check-artifacts:
	@[ -n "$$(ls -A "$(ARTIFACTS)/wheels" 2>/dev/null)" ] || {
	    echo "нет wheelhouse — запусти: make deps" >&2; exit 1
	}
	[ -f "$(ARTIFACTS)/tessdata/eng.traineddata" ] || {
	    echo "нет моделей OCR — запусти: make sources" >&2; exit 1
	}
	[ -n "$$(ls -A "$(ARTIFACTS)/fastembed" 2>/dev/null)" ] || {
	    echo "нет весов fastembed — запусти: make deps" >&2; exit 1
	}


# портативный каталог релиза: third/ (python, инструменты, модели, библиотеки),
# etc/, conf/, local/. Собирается стадией release внутри образа из wheelhouse и
# исходников — без сети; готовый каталог достаётся docker cp.
.PHONY: build
build: version-check check-artifacts
	@docker image inspect $(BASE_IMAGE) >/dev/null 2>&1 || {
	    echo "нет образа $(BASE_IMAGE) — собери: make sources" >&2; exit 1
	}
	echo ">>> портативный релиз $(RELEASE_NAME)"
	echo "    образ:      $(IMAGE)"
	echo "    staging:    $(STAGE_DIR)"
	echo "    на сервере: $(DEST_DIR) (этот путь зашит в boba.env, unit и shebang'ы скриптов)"
	docker build -f Dockerfile --target runtime $(NO_CACHE_TEXT) \
	  --build-arg GLIBC_IMAGE=$(GLIBC_IMAGE) \
	  --build-arg GCC_IMAGE=$(GCC_IMAGE) \
	  --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE) \
	  --build-arg PYTHON_VERSION=$(PYTHON_VERSION) \
	  --build-arg BOBA_DIR="$(DEST_DIR)" \
	  -t $(IMAGE) -t boba:latest \
	  --progress plain $(BUILD_CONTEXT)

	rm -rf "$(STAGE_DIR)"
	mkdir -p "$(STAGE_DIR)"
	cid=$$(docker create $(IMAGE))
	trap 'docker rm -f "$$cid" >/dev/null 2>&1 || true' EXIT
	docker cp "$$cid:$(DEST_DIR)/." "$(STAGE_DIR)/"
	docker rm "$$cid" >/dev/null
	trap - EXIT
	chown -R "$$(stat -c '%u:%g' "$(STAGE_DIR)")" "$(STAGE_DIR)"

	# текстовые шаблоны — envsubst (читает stdin) с путём релиза ${BOBA_DIR}.
	# В config.toml остаются TOML-интерполяции (${agent.default}, ...) — envsubst
	# с явным списком переменных их не трогает.
	BOBA_DIR="$(DEST_DIR)" \
	  envsubst '$${BOBA_DIR}' \
	  < "$(DEPLOY_DIR)/boba-chainlit.service" > "$(STAGE_DIR)/conf/boba-chainlit.service"
	BOBA_DIR="$(DEST_DIR)" PYTHON_VERSION="$(PYTHON_VERSION)" \
	  envsubst '$${BOBA_DIR} $${PYTHON_VERSION}' \
	  < "$(DEPLOY_DIR)/boba.env" > "$(STAGE_DIR)/conf/boba.env"
	BOBA_DIR="$(DEST_DIR)" \
	  envsubst '$${BOBA_DIR}' \
	  < "$(DEPLOY_DIR)/config.toml.example" > "$(STAGE_DIR)/conf/config.toml"

	{
	    echo "release: $(RELEASE_NAME)"
	    echo "version: $(VERSION)"
	    echo "python:  $(PYTHON_VERSION)"
	    echo "source:  $(SRC_DIR)"
	} > "$(STAGE_DIR)/VERSION.txt"

	echo ""
	echo ">>> релиз готов: $(STAGE_DIR)  ($$(du -sh --apparent-size "$(STAGE_DIR)" 2>/dev/null | cut -f1))"
	echo "    образ $(IMAGE) — для dev compose и запуска"
	echo "    заполнить conf/config.toml и conf/prompts/, затем: make export"


# практически просто копирует собранный каталог в $(DEST_DIR).
# Если каталог не пишется текущим пользователем — через sudo.
.PHONY: export
export:
	@[ -d "$(STAGE_DIR)/third" ] || {
	    echo "нет собранного релиза — сначала: make build" >&2; exit 1
	}
	echo ">>> экспорт: $(STAGE_DIR) -> $(DEST_DIR)"
	SUDO=""; [ -w "$(dir $(DEST_DIR))" ] || SUDO=sudo
	$$SUDO rm -rf "$(DEST_DIR)"
	$$SUDO mkdir -p "$(dir $(DEST_DIR))"
	$$SUDO cp -a "$(STAGE_DIR)/." "$(DEST_DIR)"
	$$SUDO chown -R "$$(stat -c '%u:%g' "$(STAGE_DIR)")" "$(DEST_DIR)"
	echo ""
	echo ">>> готово: $(DEST_DIR)  ($$(du -sh --apparent-size "$(DEST_DIR)" 2>/dev/null | cut -f1))"
	echo "    на сервер — как есть (tar/rsync), см. README.md"


# Функциональная проверка собранного каталога релиза — в контейнере
# release-образа: там стоят системные библиотеки (как на сервере после
# apt install), которых на хосте сборки может не быть. env собирается из
# шаблона на путь $(DEST_DIR) — в контейнере он совпадает с BOBA_DIR.
.PHONY: test
test: build
	@echo ">>> проверка $(RELEASE_NAME) в контейнере (системные библиотеки Astra)"
	BOBA_DIR="$(DEST_DIR)" PYTHON_VERSION="$(PYTHON_VERSION)" \
	  envsubst '$${BOBA_DIR} $${PYTHON_VERSION}' \
	  < "$(DEPLOY_DIR)/boba.env" > "$(BASE_DIR)/test-boba.env"
	docker run --rm \
	  -v "$(TEST_DIR)":/test:ro \
	  -v "$(BASE_DIR)/test-boba.env":/test-boba.env:ro \
	  --entrypoint sh $(IMAGE) -c '
	    set -a && . /test-boba.env && set +a
	    "$$PYTHONHOME/bin/python3" /test/bobacheck.py --names /tmp/names.txt
	  '
	rm -f "$(BASE_DIR)/test-boba.env"


# колёса для раздачи наружу, к образу отношения не имеют.
# --no-sources проверяет, что пакет соберётся и вне workspace
.PHONY: dist
dist: version-check
	@echo ">>> версия пакетов: $(VERSION)"
	rm -rf "$(BASE_DIR)/dist"
	cd "$(SRC_DIR)"
	"$(ARTIFACTS)/uv/uv" build --all-packages --wheel --no-sources -o "$(BASE_DIR)/dist"
	echo ">>> wheels в build/dist:"
	ls -1 "$(BASE_DIR)/dist"


# Удалить скачанные источники (tessdata + бинарь uv) и собранный релиз.
# wheels/fastembed НЕ трогаем.
.PHONY: clean
clean:
	rm -rf $(ARTIFACTS)/tessdata $(ARTIFACTS)/uv/uv $(STAGE_DIR)


.PHONY: print-tag
print-tag:
	@echo $(BOBA_TAG)


.PHONY: all
all: sources deps build test
