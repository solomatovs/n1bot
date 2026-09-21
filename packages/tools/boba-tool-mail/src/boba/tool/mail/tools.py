"""Tool mail: запуск отправки mail"""

from __future__ import annotations

import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from typing import Annotated, ClassVar, Final

from pydantic import ConfigDict, Field, SecretStr

from boba.toolkit.entry import ToolMain
from boba.toolkit.facade import Injected, tool
from boba.toolkit.result import (
    MarkdownResult,
)
from boba.toolkit.types import SecretRevealing


class SmtpConfig(SecretRevealing):
    """Конфиг mail инструмента"""

    model_config = ConfigDict(extra="ignore")

    SECTION: ClassVar[str] = "tool.mail"

    server: str = Field(min_length=1)
    port: int = Field(default=587, ge=1)
    use_tls: bool = Field(default=False)
    user: str = Field(min_length=1)
    password: SecretStr = Field(min_length=1)
    sender: str = Field(min_length=1)
    recipient: str = Field(min_length=1)


@tool
def mail(
    address: Annotated[
        str,
        Field(
            min_length=1,
            description="emal адрес на который отправить сообщение",
        ),
    ],
    cfg: Annotated[SmtpConfig, Injected],
) -> MarkdownResult:
    """Выполнить отправку почты email."""
    """Выполнить отправку email"""
    msg = MIMEMultipart()
    msg["From"] = cfg.sender
    msg["To"] = cfg.recipient
    msg["Subject"] = "test llm"

    with smtplib.SMTP(cfg.server, cfg.port) as server:
        if cfg.use_tls:
            server.starttls()

        if not cfg.user:
            raise RuntimeError("choose user name")

        if not cfg.password:
            raise RuntimeError("choose user password")

        password = cfg.password.get_secret_value()

        server.login(cfg.user, password)

        server.send_message(msg)

    return MarkdownResult(
        text="echo 'Hello world'",
        language="bash",
    )


TOOLS: Final = ToolMain.toolset(mail)

if __name__ == "__main__":
    sys.exit(ToolMain.run(TOOLS))
