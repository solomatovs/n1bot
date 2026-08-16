"""Формулировка сбоя: чат, история и лог обязаны говорить одно и то же.

Пользователь пересказывает текст в задачу, LLM правит по нему следующий шаг,
инженер ищет по нему в журнале — расхождение стоит часов поиска.
"""

from __future__ import annotations

from boba.toolkit.failure import FailureText


class TestSingleError:
    def test_type_and_message(self) -> None:
        assert FailureText.of(ValueError("плохой ввод")) == "ValueError: плохой ввод"

    def test_empty_message_leaves_the_type(self) -> None:
        """У части библиотечных исключений текста нет — остаётся имя типа."""
        assert FailureText.of(TimeoutError()) == "TimeoutError"


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

        assert described == (
            "RuntimeError: Connection error. "
            "<- OSError: All connection attempts failed"
        )

    def test_implicit_context_is_appended(self) -> None:
        """raise внутри except без from: причина всё равно известна."""
        try:
            try:
                raise KeyError("host")
            except KeyError:
                raise RuntimeError("lookup failed")  # noqa: B904
        except RuntimeError as error:
            described = FailureText.of(error)

        assert "RuntimeError: lookup failed" in described
        assert "KeyError: 'host'" in described

    def test_suppressed_context_is_dropped(self) -> None:
        """`from None` — автор явно сказал, что причина не относится к делу."""
        try:
            try:
                raise KeyError("host")
            except KeyError:
                raise RuntimeError("clean") from None
        except RuntimeError as error:
            described = FailureText.of(error)

        assert described == "RuntimeError: clean"

    def test_chain_is_bounded(self) -> None:
        """Длинная цепочка обёрток не должна раздувать сообщение в чате."""
        error: Exception = ValueError("root")
        for index in range(10):
            wrapper = RuntimeError(f"layer {index}")
            wrapper.__cause__ = error
            error = wrapper

        described = FailureText.of(error)

        assert described.count(FailureText.SEPARATOR) == FailureText.MAX_LINKS - 1

    def test_self_reference_does_not_loop(self) -> None:
        error = RuntimeError("loop")
        error.__cause__ = error

        assert FailureText.of(error) == "RuntimeError: loop"
