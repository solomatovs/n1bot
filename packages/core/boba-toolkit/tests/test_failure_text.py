"""Формулировка сбоя: чат, история и лог обязаны говорить одно и то же.

Пользователь пересказывает текст в задачу, LLM правит по нему следующий шаг,
инженер ищет по нему в журнале — расхождение стоит часов поиска.
"""

from __future__ import annotations

from boba.toolkit.failure import FailureText


class TestSingleError:
    def test_type_and_message(self) -> None:
        if FailureText.of(ValueError("плохой ввод")) != "ValueError: плохой ввод":
            raise AssertionError('FailureText.of(ValueError("плохой ввод")) == "Value…')

    def test_empty_message_leaves_the_type(self) -> None:
        """У части библиотечных исключений текста нет — остаётся имя типа."""
        if FailureText.of(TimeoutError()) != "TimeoutError":
            raise AssertionError('FailureText.of(TimeoutError()) == "TimeoutError"')


class TestCauseChain:
    """Причина объясняет сбой: без неё «Connection error.» ничего не говорит."""

    def test_explicit_cause_is_appended(self) -> None:
        try:
            try:
                raise OSError("All connection attempts failed")
            except OSError as low:
                raise RuntimeError("Connection error.") from low
        except RuntimeError as error:
            described = FailureText.of(error)

        if not (
            described
            == (
                "RuntimeError: Connection error. "
                "<- OSError: All connection attempts failed"
            )
        ):
            raise AssertionError('described == ( "RuntimeError: Connection error. " "…')

    def test_implicit_context_is_appended(self) -> None:
        """raise внутри except без from: причина всё равно известна."""
        try:
            try:
                raise KeyError("host")
            except KeyError:
                raise RuntimeError("lookup failed")  # noqa: B904
        except RuntimeError as error:
            described = FailureText.of(error)

        if "RuntimeError: lookup failed" not in described:
            raise AssertionError('"RuntimeError: lookup failed" in described')
        if "KeyError: 'host'" not in described:
            raise AssertionError("\"KeyError: 'host'\" in described")

    def test_suppressed_context_is_dropped(self) -> None:
        """`from None` — автор явно сказал, что причина не относится к делу."""
        try:
            try:
                raise KeyError("host")
            except KeyError:
                raise RuntimeError("clean") from None
        except RuntimeError as error:
            described = FailureText.of(error)

        if described != "RuntimeError: clean":
            raise AssertionError('described == "RuntimeError: clean"')

    def test_chain_is_bounded(self) -> None:
        """Длинная цепочка обёрток не должна раздувать сообщение в чате."""
        error: Exception = ValueError("root")
        for index in range(10):
            wrapper = RuntimeError(f"layer {index}")
            wrapper.__cause__ = error
            error = wrapper

        described = FailureText.of(error)

        if described.count(FailureText.SEPARATOR) != FailureText.MAX_LINKS - 1:
            raise AssertionError("described.count(FailureText.SEPARATOR) == FailureTe…")

    def test_self_reference_does_not_loop(self) -> None:
        error = RuntimeError("loop")
        error.__cause__ = error

        if FailureText.of(error) != "RuntimeError: loop":
            raise AssertionError('FailureText.of(error) == "RuntimeError: loop"')
