"""Проверяет сохранение ошибки при отмене TaskGroup в сборке Nuitka."""

import anyio
from anyio._backends import _asyncio


class CancellationProbe:
    """Проверяет выход AnyIO из вложенных задач с первичной ошибкой MCP."""

    async def run(self) -> None:
        if not hasattr(_asyncio, "__compiled__"):
            raise AssertionError("The probe requires a compiled AnyIO asyncio backend")

        try:
            async with (
                anyio.create_task_group(),
                anyio.create_task_group() as inner,
            ):
                inner.cancel_scope.cancel()
                raise ValueError("MCP endpoint returned HTTP 404")
        except ExceptionGroup as exc:
            self._check(exc)
        else:
            raise AssertionError("The original MCP failure was lost")

        async with anyio.create_task_group() as cancelled:
            cancelled.cancel_scope.cancel()
            await anyio.sleep(0)

        if not cancelled.cancel_scope.cancelled_caught:
            raise AssertionError("TaskGroup cancellation was not handled")

    def _check(self, error: Exception) -> None:
        if isinstance(error, ExceptionGroup):
            if len(error.exceptions) != 1:
                raise AssertionError(f"Unexpected exception group: {error!r}")

            self._check(error.exceptions[0])
            return

        if not isinstance(error, ValueError):
            raise AssertionError(
                f"The MCP failure was replaced by {error!r}"
            ) from error

        if str(error) != "MCP endpoint returned HTTP 404":
            raise AssertionError(f"The MCP failure changed: {error!r}") from error


if __name__ == "__main__":
    anyio.run(CancellationProbe().run)
    print("Compiled AnyIO preserves the MCP failure and handles cancellation")
