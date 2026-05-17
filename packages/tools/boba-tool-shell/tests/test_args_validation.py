"""Чистая валидация Args-моделей: без subprocess и без bwrap.

Покрывает обе модели (`BashArgs`, `BashSandboxArgs`), чтобы случайная
расходимость их валидаторов ловилась тестами.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from boba.tool.shell.bash_local import BashArgs
from boba.tool.shell.bash_sandbox import BashSandboxArgs


@pytest.mark.parametrize("model", [BashArgs, BashSandboxArgs])
def test_command_empty_rejected(model):
    with pytest.raises(ValidationError, match="пустым"):
        model(command="   ")


@pytest.mark.parametrize("model", [BashArgs, BashSandboxArgs])
def test_command_unclosed_quote_rejected(model):
    with pytest.raises(ValidationError, match="shell-токенизации"):
        model(command='echo "still open')


@pytest.mark.parametrize("model", [BashArgs, BashSandboxArgs])
def test_command_with_shell_metachars_accepted(model):
    # shlex.split — это лексический pre-check, а не семантическая
    # песочница: пайпы/перенаправления/$()-substitution разрешены,
    # их интерпретирует bash. Изоляцию даёт sandbox-вариант.
    args = model(command="echo foo | grep bar && echo done")
    assert args.command == "echo foo | grep bar && echo done"


@pytest.mark.parametrize("model", [BashArgs, BashSandboxArgs])
def test_command_too_long_rejected(model):
    huge = "echo " + "x" * 17_000
    with pytest.raises(ValidationError):
        model(command=huge)


@pytest.mark.parametrize("model", [BashArgs, BashSandboxArgs])
def test_stdin_default_empty_string(model):
    args = model(command="echo x")
    assert args.stdin == ""


def test_sandbox_profile_default_empty_string():
    # В local-варианте поля `profile` нет — проверяем только sandbox.
    args = BashSandboxArgs(command="echo x")
    assert args.profile == ""


@pytest.mark.parametrize("model", [BashArgs, BashSandboxArgs])
def test_stdin_oversized_rejected(model):
    huge = "x" * (2 * 1024 * 1024)
    with pytest.raises(ValidationError):
        model(command="cat", stdin=huge)


@pytest.mark.parametrize("model", [BashArgs, BashSandboxArgs])
def test_extra_fields_forbidden(model):
    with pytest.raises(ValidationError):
        model(command="echo x", unknown_field="boom")
