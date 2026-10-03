"""Любое исключение — результат-ошибка: одна упаковка, один вид во всех каналах.

Пользователь пересказывает текст в задачу, LLM правит по нему следующий шаг,
инженер ищет по нему в журнале — поэтому лента, история и журнал получают
один и тот же результат, а различаются только раскладкой.
"""

from __future__ import annotations

from pydantic import BaseModel, SecretStr, ValidationError

from boba.toolkit.chain import GroupCall, GroupFailureResult
from boba.toolkit.failure import FailurePacker, ToolRefusalError
from boba.toolkit.launcher import PayloadFailureError
from boba.toolkit.protocol import REPLY, ReplyError
from boba.toolkit.result import (
    CodeBlock,
    ErrorResult,
    ExceptionResult,
    FailureResult,
    ToolArtifact,
)

PACKER = FailurePacker()


class Profile(BaseModel):
    login: str
    password: SecretStr
    port: int


def packed(error: BaseException) -> ExceptionResult:
    result = PACKER.pack(error)
    if not isinstance(result, ExceptionResult):
        raise AssertionError(f"packed as {type(result).__name__}")

    return result


def raised(error: Exception) -> Exception:
    """Исключение с настоящей трассой: как его видит граница перехвата."""
    try:
        raise error
    except Exception as caught:
        return caught


def chained() -> Exception:
    try:
        try:
            raise OSError("All connection attempts failed")
        except OSError as low:
            raise RuntimeError("Connection error.") from low
    except RuntimeError as error:
        return error


class TestSingleError:
    def test_type_and_message_make_the_headline(self) -> None:
        result = packed(raised(ValueError("плохой ввод")))

        if result.headline() != "ValueError: плохой ввод":
            raise AssertionError(f"headline: {result.headline()!r}")
        if result.error_kind != "ValueError":
            raise AssertionError(f"error_kind: {result.error_kind!r}")

    def test_empty_message_leaves_the_type(self) -> None:
        """У части библиотечных исключений текста нет — остаётся имя типа."""
        result = packed(raised(TimeoutError()))

        if result.headline() != "TimeoutError":
            raise AssertionError(f"headline: {result.headline()!r}")

    def test_raised_at_names_the_frame(self) -> None:
        result = packed(raised(KeyError("embedding")))

        if "test_failure_packer.py" not in result.raised_at:
            raise AssertionError(f"raised_at: {result.raised_at!r}")
        if not result.raised_at.endswith("in raised"):
            raise AssertionError(f"raised_at: {result.raised_at!r}")

    def test_multiline_message_keeps_alignment_in_a_block(self) -> None:
        """Стрелка `^` postgres указывает на место в строке SQL только в
        моноширинном блоке."""
        text = (
            "permission denied for schema catalog\n"
            "LINE 1: select id from catalog.pg_relations\n"
            "                       ^"
        )
        result = packed(raised(Exception(text)))

        markdown = result.chat_view().markdown
        if "**Exception: permission denied for schema catalog**" not in markdown:
            raise AssertionError(markdown)
        if "```\nLINE 1: select id from catalog.pg_relations\n" not in markdown:
            raise AssertionError(markdown)

    def test_long_line_is_clipped_in_chat_but_whole_for_llm(self) -> None:
        text = "clickhouse statement failed: " + "x " * 200
        result = packed(raised(RuntimeError(text)))

        if len(result.headline()) > ExceptionResult.HEADLINE_CHARS + 20:
            raise AssertionError(f"headline: {len(result.headline())} chars")
        if not result.headline().endswith("…"):
            raise AssertionError(f"headline: {result.headline()!r}")

        blocks = [block for block in result.details() if isinstance(block, CodeBlock)]
        if blocks[0].text != text.strip():
            raise AssertionError("chat block must carry the whole text")

        llm = result.llm_view()
        if llm.count("clickhouse statement failed") != 1:
            raise AssertionError(f"llm text repeats the message: {llm!r}")


class TestCauseChain:
    """Причина объясняет сбой: без неё «Connection error.» ничего не говорит."""

    def test_explicit_cause_is_listed(self) -> None:
        result = packed(chained())

        if [cause.error_type for cause in result.causes] != ["OSError"]:
            raise AssertionError(f"causes: {result.causes!r}")
        cause = "caused by: OSError: All connection attempts failed"
        if cause not in result.llm_view():
            raise AssertionError(result.llm_view())

    def test_implicit_context_is_listed(self) -> None:
        """raise внутри except без from: причина всё равно известна."""
        try:
            try:
                raise KeyError("host")
            except KeyError:
                raise RuntimeError("lookup failed")  # noqa: B904
        except RuntimeError as error:
            result = packed(error)

        if "KeyError: 'host'" not in result.llm_view():
            raise AssertionError(result.llm_view())

    def test_suppressed_context_is_dropped(self) -> None:
        """`from None` — автор явно сказал, что причина не относится к делу."""
        try:
            try:
                raise KeyError("host")
            except KeyError:
                raise RuntimeError("clean") from None
        except RuntimeError as error:
            result = packed(error)

        if result.causes:
            raise AssertionError(f"causes: {result.causes!r}")

    def test_repeated_message_is_named_by_type_only(self) -> None:
        """Драйвер и обёртка часто несут один текст — второй раз его не печатаем."""
        try:
            try:
                raise ConnectionError("Code: 36. DB::Exception: bad macro")
            except ConnectionError as low:
                raise RuntimeError(f"statement failed: {low}") from low
        except RuntimeError as error:
            result = packed(error)

        llm = result.llm_view()
        if llm.count("bad macro") != 1:
            raise AssertionError(llm)
        if "caused by: ConnectionError (same message)" not in llm:
            raise AssertionError(llm)

    def test_chain_is_bounded(self) -> None:
        error: Exception = ValueError("root")
        for index in range(10):
            wrapper = RuntimeError(f"layer {index}")
            wrapper.__cause__ = error
            error = wrapper

        result = packed(error)

        if len(result.causes) != FailurePacker.MAX_LINKS - 1:
            raise AssertionError(f"causes: {len(result.causes)}")

    def test_self_reference_does_not_loop(self) -> None:
        error = RuntimeError("loop")
        error.__cause__ = error

        if packed(error).causes:
            raise AssertionError("self reference must not become a cause")

    def test_trace_goes_from_root_cause_to_wrapper(self) -> None:
        result = packed(chained())

        trace = result.trace()
        if trace.index("OSError") > trace.index("RuntimeError: Connection error."):
            raise AssertionError(trace)
        if "direct cause of the following exception" not in trace:
            raise AssertionError(trace)
        if trace not in result.log_view():
            raise AssertionError("log view must carry the trace")
        if "Traceback" in result.llm_view():
            raise AssertionError("llm view must not carry the trace")


class TestSecrets:
    def test_validation_error_input_is_not_shown(self) -> None:
        """str(ValidationError) печатает вход целиком — пароль ушёл бы в чат.

        Пароль собирается в рантайме: трасса печатает строки исходника, и
        литерал в них был бы не утечкой входа, а текстом самого теста.
        """
        secret = "".join(["hun", "ter2"])
        raw = {"login": "svc", "password": secret, "port": "x"}
        result: ExceptionResult | None = None
        try:
            Profile.model_validate(raw)
        except ValidationError as error:
            result = packed(error)

        if not result:
            raise AssertionError("result is None")

        for text in (result.llm_view(), result.log_view(), result.chat_view().markdown):
            if secret in text:
                raise AssertionError(f"secret leaked: {text!r}")

        if "port: Input should be a valid integer" not in result.llm_view():
            raise AssertionError(result.llm_view())


class TestReportedErrors:
    def test_refusal_passes_its_own_text(self) -> None:
        refusal = ToolRefusalError("sign_in", "sign in through Kerberos SSO instead")

        result = PACKER.pack(refusal)

        if not isinstance(result, ErrorResult):
            raise AssertionError(f"packed as {type(result).__name__}")
        if result.llm_view() != "sign in through Kerberos SSO instead":
            raise AssertionError(result.llm_view())
        if result.error_kind != "sign_in":
            raise AssertionError(result.error_kind)

    def test_payload_failure_is_not_repacked(self) -> None:
        """Ошибка, упакованная в процессе инструмента, доезжает до чата как есть."""
        inner = packed(chained())

        result = PACKER.pack(PayloadFailureError(inner))

        if result != inner:
            raise AssertionError(f"repacked: {result!r}")


class TestEnvelopeAndHistory:
    def test_failure_crosses_the_envelope_by_kind(self) -> None:
        failure = packed(chained())

        reply = REPLY.validate_json(ReplyError(failure=failure).model_dump_json())

        if not isinstance(reply, ReplyError):
            raise AssertionError(f"reply: {reply!r}")
        if reply.failure != failure:
            raise AssertionError(f"failure: {reply.failure!r}")

    def test_non_failure_kind_is_rejected_by_the_envelope(self) -> None:
        raw = '{"status": "error", "failure": {"kind": "markdown", "text": "x"}}'

        try:
            REPLY.validate_json(raw)
        except ValidationError as error:
            if "is not a FailureResult" not in str(error):
                raise AssertionError(str(error)) from error
            return

        raise AssertionError("markdown result must not pass as a failure")

    def test_history_revives_the_failure(self) -> None:
        failure = packed(chained())

        revived = ToolArtifact.revive(failure.model_dump(mode="json"))

        if not isinstance(revived, FailureResult):
            raise AssertionError(f"revived: {revived!r}")
        if revived.chat_view() != failure.chat_view():
            raise AssertionError("revived failure renders differently")


class TestGroupFailure:
    CALLS = (
        GroupCall(tool="pg_stream_out", call_id="call_00"),
        GroupCall(tool="ch_stream_in", call_id="call_01"),
    )

    def group(self, *, own: bool) -> GroupFailureResult:
        return GroupFailureResult(
            error_kind="stream_group_failed",
            cause=packed(raised(RuntimeError("Code: 36. DB::Exception: bad macro"))),
            origin=self.CALLS[1],
            calls=self.CALLS,
            own=own,
        )

    def test_origin_shows_its_error_and_the_stopped_calls(self) -> None:
        markdown = self.group(own=True).chat_view().markdown

        if "**RuntimeError: Code: 36. DB::Exception: bad macro**" not in markdown:
            raise AssertionError(markdown)
        if "nothing was committed by: pg_stream_out" not in markdown:
            raise AssertionError(markdown)

    def test_stopped_call_points_to_the_origin(self) -> None:
        result = self.group(own=False)

        if result.headline() != "stopped: ch_stream_in failed in the stream group":
            raise AssertionError(result.headline())
        if "failed call: ch_stream_in (call_01)" not in result.llm_view():
            raise AssertionError(result.llm_view())
        if result.trace():
            raise AssertionError("stopped call must not repeat the origin trace")

    def test_group_level_failure_is_shown_to_everyone(self) -> None:
        stalled = ErrorResult(
            message="channel 'rows' stalled", error_kind="stream_stalled"
        )
        result = GroupFailureResult(
            error_kind="stream_group_failed", cause=stalled, calls=self.CALLS
        )

        if result.headline() != "channel 'rows' stalled":
            raise AssertionError(result.headline())
        if "pg_stream_out, ch_stream_in" not in result.llm_view():
            raise AssertionError(result.llm_view())
