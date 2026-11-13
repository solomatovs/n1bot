"""Форматы, которые AID умеет отображать, и разбор MIME без потери параметров."""

from dataclasses import dataclass
from email.message import Message
from enum import StrEnum


class SupportedMime(StrEnum):
    """Явный перечень форматов UI; неизвестный формат остаётся файлом."""

    PLAIN = "text/plain"
    MARKDOWN = "text/markdown"
    CSV = "text/csv"
    JSON = "application/json"
    SQL = "application/sql"
    SHELL = "text/x-shellscript"
    HTML = "text/html"
    CSS = "text/css"
    JAVASCRIPT = "text/javascript"
    APPLICATION_JAVASCRIPT = "application/javascript"
    PYTHON = "text/x-python"
    XML = "application/xml"
    TEXT_XML = "text/xml"
    YAML = "application/yaml"
    TEXT_YAML = "text/yaml"
    MERMAID = "text/vnd.mermaid"
    PDF = "application/pdf"
    PNG = "image/png"
    JPEG = "image/jpeg"
    GIF = "image/gif"
    WEBP = "image/webp"
    SVG = "image/svg+xml"
    AVIF = "image/avif"
    BMP = "image/bmp"
    ICON = "image/x-icon"
    MPEG_AUDIO = "audio/mpeg"
    MP3 = "audio/mp3"
    MP4_AUDIO = "audio/mp4"
    OGG_AUDIO = "audio/ogg"
    WAV = "audio/wav"
    X_WAV = "audio/x-wav"
    FLAC = "audio/flac"
    WEBM_AUDIO = "audio/webm"
    AAC = "audio/aac"
    MP4_VIDEO = "video/mp4"
    WEBM_VIDEO = "video/webm"
    OGG_VIDEO = "video/ogg"


class MimeProfile(StrEnum):
    """Профили, для которых AID знает отдельный способ обработки."""

    MCP_APP = "mcp-app"


@dataclass(frozen=True)
class Mime:
    """Исходный MIME для файла, нормализованный тип и отдельные параметры.

    charset и неизвестные profile не меняют обработчик основного типа.
    Только text/html с profile=mcp-app относится к расширению MCP Apps.
    Все параметры сохраняются независимо от выбранного обработчика.
    """

    value: str
    media_type: str
    parameters: tuple[tuple[str, str], ...] = ()

    @classmethod
    def parse(cls, value: str) -> "Mime":
        header = Message()
        header["content-type"] = value
        media_type = value.partition(";")[0].strip().lower()
        parameters = tuple(
            (str(key).lower(), str(val)) for key, val in (header.get_params() or [])[1:]
        )
        return cls(value=value, media_type=media_type, parameters=parameters)

    @property
    def is_mcp_app(self) -> bool:
        """Профиль MCP Apps имеет смысл только вместе с типом text/html."""
        return (
            self.media_type == SupportedMime.HTML
            and ("profile", MimeProfile.MCP_APP) in self.parameters
        )

    @property
    def supported(self) -> SupportedMime | None:
        """Формат общего renderer. MCP Apps отображает отдельный ToolApp."""
        if self.is_mcp_app:
            return None
        try:
            return SupportedMime(self.media_type)
        except ValueError:
            return None

    def utf8(self) -> "Mime":
        """ACP text сохраняется в UTF-8; заявленный charset должен совпадать."""
        if not any(key == "charset" for key, _ in self.parameters):
            return self
        header = Message()
        header["content-type"] = self.value
        header.set_param("charset", "utf-8")
        return self.parse(str(header["content-type"]))
