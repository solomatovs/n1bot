# Общее для build/chainlit/Makefile и build/studio/Makefile: всё, что зависит от хоста
# сборки, а не от приложения. Подключается после SRC_DIR и CONF_DIR.

PYTHON_VERSION  ?= 3.11

# скрипты сборки зовутся python'ом venv репозитория, системный может быть старым.
# Пока .venv нет (его создаёт dev.sh, которому нужно колесо из цели fetch), берётся
# python версии проекта из PATH
ifeq ($(origin PYTHON),undefined)
PYTHON          := $(firstword $(wildcard $(SRC_DIR)/.venv/bin/python) $(shell command -v python$(PYTHON_VERSION)))
endif
ifeq ($(PYTHON),)
$(error нет ни $(SRC_DIR)/.venv/bin/python, ни python$(PYTHON_VERSION) в PATH: поставь python $(PYTHON_VERSION) или задай PYTHON=<путь>)
endif

# --- runtime-зависимости -----------------------------------------------------------

# модели, бинарники и библиотеки песочницы, её образы. Экземпляр один на репозиторий
# и общий для приложений: его монтирует compose и читает отладка. Цели fetch,
# sandbox и plugin-rootfs любого приложения пишут в него же
RUNTIME_DIR     := $(SRC_DIR)/runtime
MODELS_DIR      := $(RUNTIME_DIR)/models
THIRD_DIR       := $(RUNTIME_DIR)/third
SANDBOX_DIR     := $(RUNTIME_DIR)/sandbox
# приложения, которые исполняют инструменты в песочнице: образы корней плагинов у них
# общие и несут точки монтирования conf/krb дерева compose каждого — оттуда keytab и
# krb5.conf читает отладка
SANDBOX_APPS    := chainlit studio
SANDBOX_DEV_KRB := $(foreach app,$(SANDBOX_APPS),$(SRC_DIR)/compose/$(app)/conf/krb)

# --- docker ---------------------------------------------------------------------

# квота CPU cgroup, в котором идёт make: квоту ставит и любой предок, поэтому берётся
# наименьшая по пути до корня; без квоты — nproc. Сам nproc квоту не видит
define cpu-quota
jobs=$$(nproc)
dir=/sys/fs/cgroup$$(sed -n 's/^0:://p' /proc/self/cgroup)
while [ "$$dir" != /sys/fs/cgroup ]; do
    if [ -r "$$dir/cpu.max" ]; then
        read -r quota period < "$$dir/cpu.max"
        if [ "$$quota" != max ]; then
            limit=$$(( (quota + period - 1) / period ))
            if [ "$$limit" -lt "$$jobs" ]; then
                jobs=$$limit
            fi
        fi
    fi
    dir=$${dir%/*}
done
echo "$$jobs"
endef

# число заданий одного компилирующего шага сборки (ninja, make, compileall, сборки и
# установки uv, esbuild фронта). BuildKit ведёт несколько стадий сразу, поэтому суммарно
# заданий бывает в два-три раза больше
ifeq ($(origin JOBS),undefined)
JOBS            := $(shell $(cpu-quota))
endif

# от кого идёт docker run с томами хоста и кому отдаются его результаты. Rootless-демон
# отображает root контейнера на пользователя демона, а его же uid внутри контейнера —
# на чужой subuid, которому каталоги хоста закрыты: там контейнер идёт от root.
# Обычному демону называется пользователь хоста, иначе результаты достались бы root
RUN_OWNER       := $(shell id -u):$(shell id -g)
ifneq ($(findstring name=rootless,$(shell docker info --format '{{.SecurityOptions}}' 2>/dev/null)),)
RUN_OWNER       := 0:0
endif

# --- окружение приложения ------------------------------------------------------------

# значение [env] конфига приложения: $(1) — config.toml, $(2) — ключ
define config-env
$(PYTHON) -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["env"][sys.argv[2]])' "$(1)" "$(2)"
endef

# конфиг окружения приложения из шаблона conf/boba.env: $(1) — корень установки,
# $(2) — каталог данных, $(3) — порт, $(4) — префикс url
define app-env
export BOBA_BASE="$(1)" BOBA_VERSION="$(VERSION)" BOBA_DATA="$(2)" \
    BOBA_PORT="$(3)" BOBA_URL_PREFIX="$(4)"
envsubst '$${BOBA_BASE} $${BOBA_VERSION} $${BOBA_DATA} $${BOBA_PORT} $${BOBA_URL_PREFIX}' < "$(CONF_DIR)/boba.env"
endef

# --- дерево отладки -------------------------------------------------------------------

# конфиг отладки: sed-выражения поверх конфига compose. debug-env правит ключ только
# внутри секции [env]; пути зависимостей заданы от base (каталог debug/<приложение>)
debug-env = -e '/^\[env\]/,/^\[site\]/s|^\( *$(1) *= *\).*|\1$(2)|'
DEBUG_CGROUP    := /sys/fs/cgroup/user.slice/user-$(shell id -u).slice/user@$(shell id -u).service/boba.slice/boba-debug.slice/boba-sandbox@debug.service/sandbox
DEBUG_ENV_COMMON = $(call debug-env,port,$(DEBUG_PORT))                               \
    $(call debug-env,url_prefix,"$(DEBUG_PREFIX)")                                    \
    $(call debug-env,instance_id,"dev")                                               \
    $(call debug-env,messaging_provider,"local")                                      \
    $(call debug-env,tool_launcher,"process")                                         \
    $(call debug-env,cgroup_base,"$(DEBUG_CGROUP)")                                   \
    $(call debug-env,models,"$${env.base}/../../runtime/models")                      \
    $(call debug-env,sandbox,"$${env.base}/../../runtime/sandbox")                    \
    $(call debug-env,third,"$${env.base}/../../runtime/third")                        \
    $(call debug-env,krb,"$${env.base}/../../compose/$(APP)/conf/krb")                \
    $(call debug-env,app_root,"$${env.base}/../../$(DEBUG_APP_ROOT)")
# ключи [env], которые конфиг отладки обязан нести: sed молча пропускает ключ,
# которого нет в конфиге compose
DEBUG_ENV_KEYS  := port url_prefix instance_id messaging_provider tool_launcher cgroup_base \
                   data models sandbox third krb app_root

# дерево отладки на хосте: свой конфиг и свои данные, чтобы процесс на хосте и
# контейнер compose не писали в одни каталоги. Конфиг — копия конфига compose с
# отладочными значениями, прописанными в файле; готовый конфиг не перезаписывается.
# Модели, third и образы песочницы читаются из runtime/, статика — из ассетов пакета,
# keytab и krb5.conf — из conf/krb дерева compose. $(1) — sed-выражения приложения
define debug-tree
$(call check,$(DEV_OUT)/conf/config.toml,положи конфиг приложения $(APP))
$(call check,$(SANDBOX_PARTS),запусти: make sandbox)
echo "== дерево отладки $(APP) в $(OUT) =="
umask 077
mkdir -p "$(OUT)/conf" "$(OUT)/data/workspace" "$(OUT)/data/tool-logs" "$(OUT)/data/dump" "$(OUT)/data/krb"
if [ -e "$(OUT)/conf/config.toml" ]; then
    echo "    конфиг уже есть, не трогаю: $(OUT)/conf/config.toml"
else
    sed $(DEBUG_ENV_COMMON) $(1) "$(DEV_OUT)/conf/config.toml" > "$(OUT)/conf/config.toml"
    if [ -d "$(DEV_OUT)/conf/plugins" ]; then
        cp -r "$(DEV_OUT)/conf/plugins" "$(OUT)/conf/plugins"
    fi
    echo "    конфиг отладки: $(OUT)/conf/config.toml (из $(DEV_OUT)/conf, значения отладочные)"
fi
for key in $(DEBUG_ENV_KEYS); do
    $(call config-env,$(OUT)/conf/config.toml,$$key) > /dev/null
done
echo ">>> дерево отладки готово: $(OUT)"
echo "    cgroup:  systemctl --user start boba-sandbox@debug.service (режим sandbox)"
echo "    debug:   cd $(OUT) && python -m $(APP_MODULE) --config $(OUT)/conf/config.toml"
endef
