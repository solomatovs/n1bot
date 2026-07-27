import asyncio
import contextlib
import functools
import inspect
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any, ClassVar, Literal

Scope = Literal["app", "session", "transient"]


class Depends:
    """Маркер DI-зависимости в Annotated/дефолте параметра (аналог fastapi.Depends)."""

    provider: "Callable[..., Any]"
    scope: Scope
    __slots__ = ("provider", "scope")

    def __init__(self, provider: Callable[..., Any], *, scope: Scope = "app") -> None:
        self.provider = provider
        self.scope = scope


class Container:
    """Контейнер одного scope-уровня; резолвит провайдеров вверх по иерархии."""

    # уровни контейнеров от широкого к узкому
    _RANK: ClassVar[dict[str, int]] = {"app": 0, "session": 1, "call": 2}
    # какой уровень контейнера ВЛАДЕЕТ scope'ом зависимости
    _OWNER: ClassVar[dict[Scope, str]] = {
        "app": "app",
        "session": "session",
        "transient": "call",
    }
    # transient пересоздаётся на каждый резолв, остальные кэшируются у владельца
    _CACHED: ClassVar[frozenset[Scope]] = frozenset({"app", "session"})

    # root (app) контейнер; session-хук в боксе-списке, чтобы функция не
    # связалась как метод при доступе через класс
    root: ClassVar["Container | None"] = None
    _session_hook: ClassVar[list["Callable[[], Container | None]"]] = []

    def __init__(self, level: Scope | str, parent: "Container | None" = None) -> None:
        self.level = level
        self.parent = parent
        self._cache: dict[Callable, Any] = {}
        self._locks: dict[Callable, asyncio.Lock] = {}
        self._stack = AsyncExitStack()
        self._eager: list[Depends] = []

    @classmethod
    def set_root(cls, container: "Container | None") -> None:
        """Регистрирует app-контейнер (или сбрасывает на teardown)."""
        cls.root = container

    @classmethod
    def set_session_hook(cls, hook: "Callable[[], Container | None] | None") -> None:
        """Ставит способ достать session-контейнер из текущего контекста."""
        if hook is None:
            cls._session_hook[:] = []
        else:
            cls._session_hook[:] = [hook]

    @classmethod
    def begin_call(cls) -> "Container":
        """Создаёт call-контейнер поверх session-контейнера (или root'а)."""
        if cls.root is None:
            raise RuntimeError("DI Container не инициализирован (set_root)")

        session = cls._session_hook[0]() if cls._session_hook else None
        return cls(level="call", parent=session or cls.root)

    @staticmethod
    def find_depend(param: inspect.Parameter) -> "Depends | None":
        """Достаёт Depend из Annotated-метаданных или из дефолта параметра."""
        if isinstance(param.default, Depends):
            return param.default

        for meta in getattr(param.annotation, "__metadata__", ()):
            if isinstance(meta, Depends):
                return meta

        return None

    async def get(self, provider: Callable[..., Any], *, scope: Scope = "app") -> Any:
        """Резолвит провайдера по ссылке (разовый резолв вне callback'а)."""
        return await self.resolve(Depends(provider, scope=scope))

    def provide(
        self, provider: Callable[..., Any], value: Any, *, scope: Scope = "app"
    ) -> None:
        """Засевает готовый инстанс как результат провайдера (минуя его вызов)."""
        self._owner(scope)._cache[provider] = value

    def resolved(self, provider: Callable[..., Any], *, scope: Scope = "app") -> Any:
        """
        Резолвит зависимость синхронно из кэша
        только для прогретых eager/provide провайдеров
        """
        owner = self._owner(scope)
        if provider not in owner._cache:
            name = getattr(provider, "__name__", repr(provider))
            raise RuntimeError(f"провайдер {name} не прогрет (eager/start/provide)")
        return owner._cache[provider]

    def eager(self, *providers: Callable[..., Any], scope: Scope = "app") -> None:
        """Помечает провайдеров на прогрев при start() (что греть — решает конфиг)."""
        self._eager.extend(Depends(p, scope=scope) for p in providers)

    async def start(self) -> None:
        """Прогревает помеченные eager-провайдеры; ошибки конфига всплывают тут."""
        for dep in self._eager:
            await self.resolve(dep)

    async def resolve(self, dep: "Depends", _outer: "Container | None" = None) -> Any:
        """Резолвит зависимость у владельца scope'а; кэш + лок + под-зависимости."""
        owner = self._owner(dep.scope)

        # широкий scope не вправе захватывать более узкий (висячая ссылка)
        if _outer is not None and self._RANK[owner.level] > self._RANK[_outer.level]:
            raise RuntimeError(
                f"scope-нарушение: провайдер уровня {_outer.level} зависит от "
                f"{dep.scope} (уровень {owner.level})"
            )

        # transient не кэшируется: общего состояния нет, лок не нужен — всегда свежий
        if dep.scope not in self._CACHED:
            kwargs = await self._resolve_sub_deps(dep.provider, owner)
            return await owner._produce(dep.provider, kwargs)

        # быстрый путь: уже в кэше (в т.ч. засеяно через provide) — без лока
        if dep.provider in owner._cache:
            return owner._cache[dep.provider]

        # под-зависимости резолвим ДО лока: у каждой свой лок/кэш, а так мы не
        # держим лок провайдера сквозь чужие await и не превращаем цикл в дедлок
        # (цикл по-прежнему упрётся в рекурсию, а не зависнет на локе)
        kwargs = await self._resolve_sub_deps(dep.provider, owner)

        # один производитель на (owner, provider): конкуренты ждут и берут из кэша
        async with owner._lock(dep.provider):
            # победитель гонки мог заполнить кэш, пока мы ждали лок — перепроверяем
            if dep.provider in owner._cache:
                return owner._cache[dep.provider]
            value = await owner._produce(dep.provider, kwargs)
            owner._cache[dep.provider] = value
            return value

    def _lock(self, provider: Callable) -> asyncio.Lock:
        """Лок на (этот контейнер, провайдер); создаётся лениво и атомарно."""
        lock = self._locks.get(provider)
        if lock is None:
            # setdefault атомарен в рамках loop'а: оба конкурента получат один лок
            lock = self._locks.setdefault(provider, asyncio.Lock())
        return lock

    def _owner(self, scope: Scope) -> "Container":
        """Поднимается по родителям до контейнера, владеющего scope'ом."""
        target = self._OWNER[scope]
        c: Container | None = self
        while c is not None and c.level != target:
            c = c.parent
        if c is None:
            raise RuntimeError(f"нет контейнера уровня {target} (scope={scope})")
        return c

    async def _resolve_sub_deps(
        self, provider: Callable, owner: "Container"
    ) -> dict[str, Any]:
        """Под-зависимости провайдера — резолвятся от листа, но не уже owner'а."""
        out: dict[str, Any] = {}
        for p in inspect.signature(provider).parameters.values():
            if dep := self.find_depend(p):
                out[p.name] = await self.resolve(dep, _outer=owner)
        return out

    async def _produce(self, provider: Callable, kwargs: dict[str, Any]) -> Any:
        """Вызывает провайдера; (async-)генератор → значение + teardown в свой стек."""
        if inspect.isasyncgenfunction(provider):
            agen = provider(**kwargs)
            value = await agen.__anext__()
            self._stack.push_async_callback(self._aclose, agen)
            return value
        if inspect.isgeneratorfunction(provider):
            gen = provider(**kwargs)
            value = next(gen)
            self._stack.callback(self._close, gen)
            return value
        if inspect.iscoroutinefunction(provider):
            return await provider(**kwargs)
        return provider(**kwargs)

    @staticmethod
    async def _aclose(agen: Any) -> None:
        with contextlib.suppress(StopAsyncIteration):
            await agen.__anext__()

    @staticmethod
    def _close(gen: Any) -> None:
        with contextlib.suppress(StopIteration):
            next(gen)

    async def aclose(self) -> None:
        """Teardown generator-провайдеров этого контейнера (LIFO) и сброс кэша."""
        await self._stack.aclose()
        self._cache.clear()
        self._locks.clear()


def di_inject(fn: Callable) -> Callable:
    """
    Резолвит Depend-параметры из иерархии контейнеров (app/session/transient)
    framework-аргументы проходят насквозь; call-контейнер закрывается по выходу.

    Async-функция -> async-шим (полный резолв с call-контейнером). Sync-функция
    -> sync-шим: синхронно достаёт уже прогретые (eager/provide) зависимости из
    кэша — для точек, которые нельзя await'ить (напр. chainlit @cl.data_layer).
    """
    sig = inspect.signature(fn)
    deps = {
        name: dep
        for name, p in sig.parameters.items()
        if (dep := Container.find_depend(p))
    }
    framework_params = [p for name, p in sig.parameters.items() if name not in deps]

    @functools.wraps(fn)
    async def async_shim(*args: Any, **kwargs: Any) -> Any:
        call = Container.begin_call()
        try:
            resolved = {name: await call.resolve(dep) for name, dep in deps.items()}
            return await fn(*args, **kwargs, **resolved)
        finally:
            await call.aclose()

    @functools.wraps(fn)
    def sync_shim(*args: Any, **kwargs: Any) -> Any:
        if Container.root is None:
            raise RuntimeError("DI Container не инициализирован (set_root)")
        # синхронно нельзя резолвить async-провайдеры — берём прогретое из кэша
        resolved = {
            name: Container.root.resolved(dep.provider, scope=dep.scope)
            for name, dep in deps.items()
        }
        return fn(*args, **kwargs, **resolved)

    shim = async_shim if inspect.iscoroutinefunction(fn) else sync_shim

    # chainlit зипует свою сигнатуру с переданными аргументами (message/user/...);
    # прячем Depend-параметры, оставляя только framework-аргументы (setattr —
    # т.к. functools.wraps типизирует shim как _Wrapped без __signature__)
    setattr(shim, "__signature__", sig.replace(parameters=framework_params))  # noqa: B010
    return shim
