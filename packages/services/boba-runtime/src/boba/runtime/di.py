"""Контейнер зависимостей: резолв Depend-параметров по уровням app/session/call."""

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

    _RANK: ClassVar[dict[str, int]] = {"app": 0, "session": 1, "call": 2}
    _OWNER: ClassVar[dict[Scope, str]] = {
        "app": "app",
        "session": "session",
        "transient": "call",
    }
    _CACHED: ClassVar[frozenset[Scope]] = frozenset({"app", "session"})

    SESSION_KEY: ClassVar[str] = "_di_session_container"
    """Ключ сессии приложения, под которым живёт контейнер сессии."""

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
        cls.root = container

    @classmethod
    def set_session_hook(cls, hook: "Callable[[], Container | None] | None") -> None:
        if hook is None:
            cls._session_hook[:] = []
        else:
            cls._session_hook[:] = [hook]

    @classmethod
    def begin_call(cls) -> "Container":
        if cls.root is None:
            msg = (
                "DI begin_call: the root container is not initialised, "
                "Container.set_root must run first"
            )
            raise RuntimeError(msg)

        session = None
        if cls._session_hook:
            session = cls._session_hook[0]()
        return cls(level="call", parent=session or cls.root)

    @staticmethod
    def find_depend(param: inspect.Parameter) -> "Depends | None":
        if isinstance(param.default, Depends):
            return param.default

        for meta in getattr(param.annotation, "__metadata__", ()):
            if isinstance(meta, Depends):
                return meta

        return None

    async def get(self, provider: Callable[..., Any], *, scope: Scope = "app") -> Any:
        return await self.resolve(Depends(provider, scope=scope))

    def provide(
        self, provider: Callable[..., Any], value: Any, *, scope: Scope = "app"
    ) -> None:
        self._owner(scope)._cache[provider] = value

    def resolved(self, provider: Callable[..., Any], *, scope: Scope = "app") -> Any:
        owner = self._owner(scope)
        if provider not in owner._cache:
            name = getattr(provider, "__name__", repr(provider))
            msg = (
                f"DI resolved: provider {name} has no value in the {owner.level} "
                "container yet; it must be warmed up by eager/start or set by "
                "provide before a synchronous read"
            )
            raise RuntimeError(msg)
        return owner._cache[provider]

    def eager(self, *providers: Callable[..., Any], scope: Scope = "app") -> None:
        self._eager.extend(Depends(p, scope=scope) for p in providers)

    async def start(self) -> None:
        for dep in self._eager:
            await self.resolve(dep)

    async def resolve(self, dep: "Depends", _outer: "Container | None" = None) -> Any:
        owner = self._owner(dep.scope)

        if _outer is not None and self._RANK[owner.level] > self._RANK[_outer.level]:
            dep_name = getattr(dep.provider, "__name__", repr(dep.provider))
            msg = (
                f"DI scope violation: a provider of level {_outer.level} depends on "
                f"{dep_name} of scope {dep.scope} (level {owner.level}), which "
                "lives shorter than its dependant"
            )
            raise RuntimeError(msg)

        if dep.scope not in self._CACHED:
            kwargs = await self._resolve_sub_deps(dep.provider, owner)
            return await owner._produce(dep.provider, kwargs)

        if dep.provider in owner._cache:
            return owner._cache[dep.provider]

        kwargs = await self._resolve_sub_deps(dep.provider, owner)

        async with owner._lock(dep.provider):
            if dep.provider in owner._cache:
                return owner._cache[dep.provider]
            value = await owner._produce(dep.provider, kwargs)
            owner._cache[dep.provider] = value
            return value

    def _lock(self, provider: Callable) -> asyncio.Lock:
        lock = self._locks.get(provider)
        if lock is None:
            lock = self._locks.setdefault(provider, asyncio.Lock())
        return lock

    def _owner(self, scope: Scope) -> "Container":
        target = self._OWNER[scope]
        c: Container | None = self
        while c is not None and c.level != target:
            c = c.parent
        if c is None:
            msg = (
                f"DI container of level {target} for scope {scope} is not in the "
                f"parent chain of the {self.level} container"
            )
            raise RuntimeError(msg)
        return c

    async def _resolve_sub_deps(
        self, provider: Callable, owner: "Container"
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for p in inspect.signature(provider).parameters.values():
            if dep := self.find_depend(p):
                out[p.name] = await self.resolve(dep, _outer=owner)
        return out

    async def _produce(self, provider: Callable, kwargs: dict[str, Any]) -> Any:
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
        await self._stack.aclose()
        self._cache.clear()
        self._locks.clear()


def di_inject(fn: Callable) -> Callable:
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
            msg = (
                f"DI inject of {fn.__name__}: the root container is not "
                "initialised, Container.set_root must run first"
            )
            raise RuntimeError(msg)
        resolved = {
            name: Container.root.resolved(dep.provider, scope=dep.scope)
            for name, dep in deps.items()
        }
        return fn(*args, **kwargs, **resolved)

    if inspect.iscoroutinefunction(fn):
        shim = async_shim
    else:
        shim = sync_shim

    setattr(shim, "__signature__", sig.replace(parameters=framework_params))  # noqa: B010
    return shim
