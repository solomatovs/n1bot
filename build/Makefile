BASE_DIR      	?= $(abspath .)
SRC_DIR       	?= $(abspath ..)
ARTIFACTS     	?= $(BASE_DIR)/artifacts
BUILD_CONTEXT 	?= $(SRC_DIR)
TEST_DIR		?= $(BASE_DIR)/test

# конфиг и keytab, с которыми гоняются pytest-тесты пакетов (см. цель pytest)
PYTEST_CONFIG	?= $(BASE_DIR)/local/hermes/config.toml
PYTEST_KEYTAB	?= $(BASE_DIR)/local/keytab/boba-svc.keytab
PYTEST_KRB5CONF	?= $(BASE_DIR)/local/keytab/krb5.conf
# сеть, в которой резолвится postgres и доступен KDC
PYTEST_NETWORK	?= docker
# что гоняем; можно сузить: make pytest PYTEST_TARGET=packages/agents/boba-hermes
PYTEST_TARGET	?= packages/agents/boba-hermes
PYTEST_ARGS		?= -q


.DEFAULT_GOAL := help


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


# PEP 440-версия пакетов — СТРОГО из git-тега release-X.Y.Z / dev-X.Y.Z
SCMVERSION := sh $(BASE_DIR)/scmversion.sh $(SRC_DIR)
ifneq ($(strip $(BOBA_SCM_VERSION)),)
  BOBA_SCM_VERSION_SHOW := $(BOBA_SCM_VERSION)
else
  BOBA_SCM_VERSION_SHOW := $(shell $(SCMVERSION) 2>/dev/null)
  ifeq ($(strip $(BOBA_SCM_VERSION_SHOW)),)
    BOBA_SCM_VERSION_SHOW := <нет валидного тега release-X.Y.Z / dev-X.Y.Z>
  endif
endif


# версия python — берётся из образа-дистрибутива dmp/python
PYTHON_VERSION	?= 3.11
# тулчейн gcc — рантайм-база (glibc + gcc8 + libstdc++)
GCC_IMAGE    	?= dmp/gcc:8.5.0
# python-дистрибутив (дерево /opt/python), накатывается поверх gcc
PYTHON_IMAGE 	?= dmp/python:$(PYTHON_VERSION)-dist
# выполнять ли сброс кэша: docker build --no-cache
NO_CACHE		?= 0

# OCR-модели tesseract (скачиваются в make sources)
TESS_LANGS    	?= eng rus osd
TESSDATA_REPO 	?= https://github.com/tesseract-ocr/tessdata_best/raw/main
# embedding-веса fastembed (скачиваются в make fastembed)
EMBED_MODELS  	?= intfloat/multilingual-e5-large

# uv — готовый статический бинарь (НЕ компилируется, в отличие от wheels).
# Платформа = выбор релиза под arch+libc: musl = статический, без зависимости от glibc.
# Для внутреннего mirror достаточно переопределить UV_URL.
UV_VERSION  	?= 0.11.21
UV_PLATFORM 	?= x86_64-unknown-linux-musl
UV_URL      	?= https://github.com/astral-sh/uv/releases/download/$(UV_VERSION)/uv-$(UV_PLATFORM).tar.gz

# базовое название для образа
IMAGE			:= boba:$(BOBA_TAG)
NO_CACHE_TEXT	:= $(if $(filter 1,$(NO_CACHE)),--no-cache,)

# закрытый контур: подхватываем ca-chain.crt, если положили в artifacts
CA_CHAIN := $(wildcard $(ARTIFACTS)/ca-chain.crt)


.PHONY: help
help:
	@echo "make (тег образа: $(BOBA_TAG)):"
	@echo "  make all        собрать всё: sources base wheels fastembed build test"
	@echo "  make sources    скачать OCR-модели tesseract и бинарь uv (нужен инет)"
	@echo "  make base       собрать базовый образ boba-base ($(GCC_IMAGE) + $(PYTHON_IMAGE))"
	@echo "  make wheels     пересобрать offline-wheels внутри boba-base по ../packages"
	@echo "  make fastembed  скачать embedding-веса в artifacts/fastembed (нужны base+wheels+инет; ДО build)"
	@echo "  make build      собрать runtime $(IMAGE) (+ boba:latest), offline из wheels"
	@echo "  make test       проверить образ boba:latest: python, uv, пакеты, OCR, инструменты"
	@echo "  make pytest     прогнать тесты пакетов в сети $(PYTEST_NETWORK) (postgres по kerberos)"
	@echo "  make dist       собрать wheel'ы всех пакетов на хосте в build/dist (без Docker)"
	@echo "  make print-tag  показать тег, который будет присвоен образу"
	@echo ""
	@echo "  Образ базируется на dmp/gcc (glibc+gcc8) с python из dmp/python -dist."
	@echo "  Зависимости ставятся offline из artifacts/wheels через uv."
	@echo "  Версия пакетов (wheel) — строго из git-тега release-X.Y.Z / dev-X.Y.Z: $(BOBA_SCM_VERSION_SHOW)"
	@echo "  Параметры: BOBA_TAG=<тег> (запинить), BOBA_SCM_VERSION=<ver>, NO_CACHE=1, GCC_IMAGE=<ref>, PYTHON_IMAGE=<ref>"
	@echo "  Полный цикл с нуля: make all"
	@echo ""


.PHONY: debug
debug:
	$(info BASE_DIR       = $(BASE_DIR))
	$(info SRC_DIR        = $(SRC_DIR))
	$(info ARTIFACTS      = $(ARTIFACTS))
	$(info BUILD_CONTEXT  = $(BUILD_CONTEXT))
	$(info TEST_DIR       = $(TEST_DIR))
	$(info PYTEST_TARGET  = $(PYTEST_TARGET))
	$(info PYTEST_NETWORK = $(PYTEST_NETWORK))
	$(info PYTEST_CONFIG  = $(PYTEST_CONFIG))
	$(info PYTEST_KEYTAB  = $(PYTEST_KEYTAB))
	$(info BOBA_TAG       = $(BOBA_TAG))
	$(info BOBA_SCM_VERSION = $(BOBA_SCM_VERSION_SHOW))
	$(info IMAGE          = $(IMAGE))
	$(info GCC_IMAGE      = $(GCC_IMAGE))
	$(info PYTHON_IMAGE   = $(PYTHON_IMAGE))
	$(info NO_CACHE       = $(NO_CACHE))
	$(info NO_CACHE_TEXT  = $(NO_CACHE_TEXT))
	$(info UV_URL         = $(UV_URL))
	$(info CA_CHAIN       = $(CA_CHAIN))
	@true


# Скачать то, что качается заранее: OCR-модели tesseract + бинарь uv.
# Идемпотентно (уже скачанное пропускает). Внутренний mirror — через переопределение URL.
# gcc/python/glibc НЕ качаем и НЕ собираем: они уже в образах dmp/gcc и dmp/python.
.PHONY: sources
sources:
	@mkdir -p $(ARTIFACTS)/tessdata $(ARTIFACTS)/bin
	@echo ">>> OCR-модели tesseract"
	@set -e; for lang in $(TESS_LANGS); do                                                     \
	  dest="$(ARTIFACTS)/tessdata/$$lang.traineddata";                                         \
	  if [ -f "$$dest" ]; then echo "  ✓ $$dest";                                              \
	  else echo ">>> $$lang"; curl -fSL -o "$$dest" "$(TESSDATA_REPO)/$$lang.traineddata"; fi; \
	done
	@echo ">>> uv ($(UV_PLATFORM))"
	@if [ -f "$(ARTIFACTS)/bin/uv" ]; then echo "  ✓ $(ARTIFACTS)/bin/uv"; \
	else echo ">>> $(UV_URL)";                                             \
	  curl -fSL "$(UV_URL)" | tar -xz -C /tmp;                             \
	  install -m 0755 /tmp/uv-$(UV_PLATFORM)/uv $(ARTIFACTS)/bin/uv;       \
	  rm -rf /tmp/uv-$(UV_PLATFORM);                                       \
	  $(ARTIFACTS)/bin/uv --version; fi


# Базовый образ boba-base: gcc-тулчейн + python + uv, без установленных пакетов.
# Нужен для offline-пересборки wheels и скачивания весов fastembed.
.PHONY: base
base:
	docker build -f Dockerfile --target base $(NO_CACHE_TEXT) \
		--build-arg GCC_IMAGE=$(GCC_IMAGE)                \
		--build-arg PYTHON_IMAGE=$(PYTHON_IMAGE)          \
		--tag boba-base:latest                            \
		--progress plain $(BUILD_CONTEXT)


# Пересобрать offline-wheels в artifacts/wheels по всем pyproject.toml из ../packages.
# Каждый pyproject.toml внутри packages/ — это отдельный пакет.
.PHONY: wheels
wheels:
	@ver="$${BOBA_SCM_VERSION:-}";                                                                               \
	  if [ -z "$$ver" ]; then ver="$$($(SCMVERSION))" || exit 1; fi;                                             \
	  echo ">>> версия пакетов: $$ver";                                                                          \
	  docker run --rm                                                                                            \
	  -e HOST_UID="$$(id -u)" -e HOST_GID="$$(id -g)"                                                             \
	  -e SETUPTOOLS_SCM_PRETEND_VERSION="$$ver"                                                                   \
	  $(if $(CA_CHAIN),-v "$(ARTIFACTS)/ca-chain.crt":/tmp/ca-chain.crt:ro                                        \
	    -e PIP_CERT=/tmp/ca-chain.crt -e REQUESTS_CA_BUNDLE=/tmp/ca-chain.crt -e SSL_CERT_FILE=/tmp/ca-chain.crt) \
	  -v "$(ARTIFACTS)":/artifacts                                                                                \
	  -v "$(SRC_DIR)":/boba:ro                                                                                    \
	  $(if $(wildcard $(ARTIFACTS)/pip.conf),-v "$(ARTIFACTS)/pip.conf":/etc/pip.conf:ro)                         \
	  -w /artifacts --entrypoint sh boba-base:latest -c '                                                         \
	    set -e; rm -rf wheels && mkdir wheels;                                                                    \
	    cp -r /boba/packages /tmp/packages;                                                                       \
	    pip3 wheel --no-cache-dir                                                                                 \
	      $$(find /tmp/packages -name pyproject.toml -printf "%h ")                                               \
	      pip setuptools setuptools_scm wheel -w wheels/;                                                         \
	    chown -R "$$HOST_UID:$$HOST_GID" wheels'


# Скачать embedding-веса fastembed (ONNX) в artifacts/fastembed/.
# Работает на boba-base: fastembed ставится offline из wheelhouse, веса качаются с HF.
# Нужны boba-base + wheels + сеть/mirror к HuggingFace; make build НЕ требуется
# (наоборот — fastembed должен быть ДО build, т.к. веса запекаются в образ).
.PHONY: fastembed
fastembed:
	@mkdir -p $(ARTIFACTS)/fastembed
	docker run --rm                                                                                                      \
	  -e HOST_UID="$$(id -u)" -e HOST_GID="$$(id -g)"                                                                    \
	  -e EMBED_MODELS="$(EMBED_MODELS)"                                                                                  \
	  -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY -e http_proxy -e https_proxy -e no_proxy                                  \
	  $(if $(CA_CHAIN),-v "$(ARTIFACTS)/ca-chain.crt":/tmp/ca-chain.crt:ro                                               \
	    -e REQUESTS_CA_BUNDLE=/tmp/ca-chain.crt -e CURL_CA_BUNDLE=/tmp/ca-chain.crt)                                     \
	  -v "$(ARTIFACTS)":/artifacts                                                                                       \
	  --entrypoint sh boba-base:latest -c '                                                                              \
	    set -e;                                                                                                          \
	    pip3 install --no-cache-dir --no-index --find-links=/artifacts/wheels fastembed;                                 \
	    for m in $$EMBED_MODELS; do echo ">>> fastembed: $$m";                                                           \
	      python3 -c "import sys; from fastembed import TextEmbedding;                                                   \
	        e=TextEmbedding(model_name=sys.argv[1], cache_dir=\"/artifacts/fastembed\");                                 \
	        list(e.embed([\"probe\"]))" "$$m"                                                                            \
	      || echo "!! $$m не скачан — не поддерживается этой версией fastembed (TextEmbedding.list_supported_models())"; \
	    done;                                                                                                            \
	    chown -R "$$HOST_UID:$$HOST_GID" /artifacts/fastembed'


# Собрать runtime-образ boba: пакеты ставятся offline из artifacts/wheels через uv.
.PHONY: build
build:
	@ver="$${BOBA_SCM_VERSION:-}";                                 \
	  if [ -z "$$ver" ]; then ver="$$($(SCMVERSION))" || exit 1; fi; \
	  echo ">>> source:   $(SRC_DIR) @ $(BOBA_TAG)";                \
	  echo ">>> version:  $$ver";                                  \
	  echo ">>> building: $(IMAGE)  (+ boba:latest)";              \
	  docker build -f Dockerfile $(NO_CACHE_TEXT)                  \
	  --build-arg GCC_IMAGE=$(GCC_IMAGE)                           \
	  --build-arg PYTHON_IMAGE=$(PYTHON_IMAGE)                     \
	  --build-arg BOBA_SCM_VERSION="$$ver"                         \
	  -t $(IMAGE) -t boba:latest                                   \
	  --progress plain $(BUILD_CONTEXT)


# Собрать wheel'ы всех пакетов на хосте в build/dist/ (строго из git-тега).
# Единственный поддерживаемый способ собрать wheel'ы без Docker: версию задаёт
# scmversion.sh (падает на чужом теге/грязи). Голый `uv build` НЕ строгий — не для релиза.
# Нужен uv в artifacts/bin (make sources) и сеть/mirror для build-зависимостей.
.PHONY: dist
dist:
	@ver="$${BOBA_SCM_VERSION:-}";                                             \
	  if [ -z "$$ver" ]; then ver="$$($(SCMVERSION))" || exit 1; fi;           \
	  echo ">>> версия пакетов: $$ver";                                        \
	  rm -rf "$(BASE_DIR)/dist";                                               \
	  cd "$(SRC_DIR)" && SETUPTOOLS_SCM_PRETEND_VERSION="$$ver"                \
	    "$(ARTIFACTS)/bin/uv" build --all-packages --wheel -o "$(BASE_DIR)/dist"; \
	  echo ">>> wheels в build/dist:"; ls -1 "$(BASE_DIR)/dist"


# Функциональная проверка собранного образа boba:latest.
.PHONY: test
test:
	docker run --rm --entrypoint /bin/bash -v $(TEST_DIR):/test:ro boba:latest -euc ' \
		echo "== image ==";                                                       \
		python3 --version;                                                        \
		uv --version;                                                             \
		echo "== functional checks ==";                                           \
		python3 /test/bobacheck.py'


# Удалить скачанные источники (tessdata + бинарь uv). wheels/fastembed НЕ трогаем.
# Тесты пакетов гоняются внутри сети docker: postgres пускает только оттуда
# (pg_hba: gss для доменных учёток), а с хоста имя postgres-17 не резолвится.
# Учётка boba-svc берётся из keytab — kinit не нужен, KRB5_CLIENT_KTNAME
# заставляет GSSAPI получить и обновлять тикет самому.
.PHONY: pytest
pytest:
	docker run --rm --network $(PYTEST_NETWORK)                                 \
		-e BOBA_CONFIG_PATH=/app/local/config.toml                              \
		-e KRB5_CLIENT_KTNAME=/etc/boba-svc.keytab                              \
		-e KRB5CCNAME=FILE:/tmp/krb5cc                                          \
		-e PGGSSENCMODE=prefer                                                  \
		-v $(PYTEST_CONFIG):/app/local/config.toml:ro                           \
		-v $(PYTEST_KEYTAB):/etc/boba-svc.keytab:ro                             \
		-v $(PYTEST_KRB5CONF):/etc/krb5.conf:ro                                 \
		-v $(SRC_DIR):$(SRC_DIR)                                                \
		-w $(SRC_DIR)                                                           \
		--entrypoint $(SRC_DIR)/.venv/bin/python                                \
		boba:latest -m pytest $(PYTEST_TARGET) $(PYTEST_ARGS)


.PHONY: clean
clean:
	rm -rf $(ARTIFACTS)/tessdata $(ARTIFACTS)/bin/uv


.PHONY: print-tag
print-tag:
	@echo $(BOBA_TAG)


.PHONY: all
all: sources base wheels fastembed build test
