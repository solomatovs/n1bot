"""Движки OCR за extra `ocr`: локальные модели PP-OCR через rapidocr на
onnxruntime (детектор строк, классификатор ориентации, распознаватель
языка; файлы моделей из каталога конфига, из сети ничего не берётся) и
vision-модель openai-совместимого endpoint'а. Секции конфига — в
boba.doc.config, движок по секции собирает OcrEngines.

Ошибки:
DocumentError — нет файлов моделей, движок не поднялся, запрос к endpoint'у
    не прошёл или распознавание сорвалось.
"""

from __future__ import annotations

import base64
import io
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

import httpx
import numpy as np
import onnxruntime
from numpy.typing import NDArray
from PIL import Image
from pydantic import BaseModel, ConfigDict, ValidationError
from rapidocr import EngineType, LangRec, ModelType, OCRVersion, RapidOCR
from rapidocr.utils.output import RapidOCROutput

from boba.doc.config import (
    DisabledOcrConfig,
    OcrModel,
    OpenAiOcrConfig,
    RapidOcrConfig,
)
from boba.doc.document import DisabledOcr, DocumentError, OcrEngine

__all__ = ["OcrEngines", "OcrLines", "OpenAiOcrEngine", "RapidOcrEngine"]


@dataclass(frozen=True)
class OcrWord:
    """Распознанный фрагмент с положением: левый край, центр и высота бокса."""

    left: float
    center_y: float
    height: float
    text: str


class OcrLines:
    """Фрагменты из боксов в строки текста: фрагмент попадает в строку, если
    его центр по вертикали лежит в пределах половины высоты строки; внутри
    строки фрагменты идут слева направо."""

    LINE_OVERLAP: ClassVar[float] = 0.5
    WORD_GLUE: ClassVar[str] = " "
    LINE_GLUE: ClassVar[str] = "\n"

    @classmethod
    def assemble(cls, boxes: NDArray[Any], texts: Sequence[str]) -> str:
        words = sorted(cls._words(boxes, texts), key=lambda word: word.center_y)
        lines: list[str] = []
        for line in cls._lines(words):
            ordered = sorted(line, key=lambda word: word.left)
            lines.append(cls.WORD_GLUE.join(word.text for word in ordered))

        return cls.LINE_GLUE.join(lines)

    @staticmethod
    def _words(boxes: NDArray[Any], texts: Sequence[str]) -> Iterator[OcrWord]:
        for box, text in zip(boxes, texts, strict=True):
            xs = box[:, 0]
            ys = box[:, 1]
            yield OcrWord(
                left=float(xs.min()),
                center_y=float((ys.min() + ys.max()) / 2),
                height=float(ys.max() - ys.min()),
                text=text,
            )

    @classmethod
    def _lines(cls, words: Sequence[OcrWord]) -> Iterator[list[OcrWord]]:
        line: list[OcrWord] = []
        for word in words:
            if not line:
                line.append(word)
                continue

            anchor = line[0]
            if abs(word.center_y - anchor.center_y) <= cls.LINE_OVERLAP * anchor.height:
                line.append(word)
                continue

            yield line
            line = [word]

        if line:
            yield line


class RapidOcrEngine(OcrEngine):
    """Реализация OcrEngine на rapidocr: модели по явным путям, вывод —
    строки текста в порядке чтения."""

    LOG_LEVEL: ClassVar[str] = "warning"

    def __init__(self, config: RapidOcrConfig) -> None:
        self._config = config
        self.check_models(config)
        try:
            self._ocr = RapidOCR(params=self._params())
        except Exception as exc:
            providers = ", ".join(onnxruntime.get_available_providers())
            raise DocumentError(
                f"ocr: initializing rapidocr with models from {config.models_dir} "
                f"for language {config.language.value!r} on onnxruntime "
                f"{onnxruntime.__version__} ({providers}) failed: {exc}"
            ) from exc

    @property
    def enabled(self) -> bool:
        return True

    def recognize(self, image: Image.Image) -> str:
        rgb = np.asarray(image.convert("RGB"))
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        try:
            output = self._ocr(bgr)
        except Exception as exc:
            raise DocumentError(
                f"ocr: recognizing {image.width}x{image.height} image failed: {exc}"
            ) from exc

        if not isinstance(output, RapidOCROutput):
            raise DocumentError(
                f"ocr: expected RapidOCROutput from rapidocr, got "
                f"{type(output).__name__}"
            )

        if output.boxes is None:
            return ""

        if output.txts is None:
            return ""

        return OcrLines.assemble(output.boxes, output.txts)

    @staticmethod
    def check_models(config: RapidOcrConfig) -> None:
        """Файлы моделей на месте; проверка без загрузки сессий, годится
        родителю до раздачи работы процессам."""
        missing: list[str] = []
        for path in OcrModel.required(config.models_dir, config.language):
            if path.is_file():
                continue

            missing.append(path.name)

        if not missing:
            return

        raise DocumentError(
            f"ocr: models directory {config.models_dir} lacks "
            f"{', '.join(missing)} for language {config.language.value!r}"
        )

    def _params(self) -> dict[str, Any]:
        models_dir = self._config.models_dir
        language = self._config.language

        return {
            "Global.log_level": self.LOG_LEVEL,
            "Global.text_score": self._config.text_score,
            "EngineConfig.onnxruntime.intra_op_num_threads": self._config.threads,
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Det.ocr_version": OCRVersion.PPOCRV5,
            "Det.model_type": ModelType.MOBILE,
            "Det.model_path": str(OcrModel.DET.path(models_dir, language)),
            "Cls.engine_type": EngineType.ONNXRUNTIME,
            "Cls.model_path": str(OcrModel.CLS.path(models_dir, language)),
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE,
            "Rec.lang_type": LangRec(language.value),
            "Rec.model_path": str(OcrModel.REC.path(models_dir, language)),
        }


class OcrPrompt(StrEnum):
    """Инструкция vision-модели и маркер пустого ответа."""

    TRANSCRIBE = (
        "Transcribe all text visible in this image exactly as written, "
        "preserving the original language, line breaks and reading order. "
        "Output only the transcribed text without any commentary or markdown. "
        "If the image contains no text, output exactly: {none}"
    )
    NONE = "<no text>"

    def render(self) -> str:
        return self.value.format(none=OcrPrompt.NONE.value)


class ChatReplyMessage(BaseModel):
    """DTO ответа /chat/completions: текст сообщения модели."""

    model_config = ConfigDict(extra="ignore")

    content: str = ""


class ChatReplyChoice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: ChatReplyMessage


class ChatReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    choices: Sequence[ChatReplyChoice]


class OpenAiOcrEngine(OcrEngine):
    """Реализация OcrEngine vision-моделью openai-совместимого endpoint'а:
    картинка уходит PNG в data-url одним сообщением, обратно приходит текст.
    Клиент синхронный — движок зовут из потоков ридеров, и он один на
    процесс, поэтому соединение переиспользуется."""

    ENDPOINT: ClassVar[str] = "chat/completions"
    IMAGE_FORMAT: ClassVar[str] = "PNG"
    IMAGE_MEDIA_TYPE: ClassVar[str] = "image/png"
    MAX_SIDE: ClassVar[int] = 2000

    def __init__(self, config: OpenAiOcrConfig) -> None:
        self._config = config
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/") + "/",
            timeout=config.timeout_sec,
            headers={"Authorization": f"Bearer {config.api_key.get_secret_value()}"},
        )

    @property
    def enabled(self) -> bool:
        return True

    def recognize(self, image: Image.Image) -> str:
        payload = self._payload(self._fit(image))
        where = f"openai ocr: POST {self._config.base_url} model {self._config.model}"
        try:
            response = self._client.post(self.ENDPOINT, json=payload)
        except httpx.HTTPError as exc:
            raise DocumentError(f"{where}: {type(exc).__name__}: {exc}") from exc

        if response.is_error:
            raise DocumentError(
                f"{where}: expected 2xx, got {response.status_code}: "
                f"{response.text[:300]}"
            )

        try:
            reply = ChatReply.model_validate_json(response.content)
        except ValidationError as exc:
            raise DocumentError(
                f"{where}: reply is not a chat completion: {exc}"
            ) from exc

        if not reply.choices:
            raise DocumentError(f"{where}: reply has no choices: {response.text[:300]}")

        text = reply.choices[0].message.content.strip()
        if text == OcrPrompt.NONE.value:
            return ""

        return text

    def _fit(self, image: Image.Image) -> Image.Image:
        """Длинная сторона не больше MAX_SIDE: токены и время под контролем."""
        longest = max(image.width, image.height)
        if longest <= self.MAX_SIDE:
            return image

        scale = self.MAX_SIDE / longest
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))

        return image.resize(size)

    def _payload(self, image: Image.Image) -> dict[str, Any]:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format=self.IMAGE_FORMAT)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        data_url = f"data:{self.IMAGE_MEDIA_TYPE};base64,{encoded}"

        return {
            "model": self._config.model,
            "max_tokens": self._config.max_tokens,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": OcrPrompt.TRANSCRIBE.render()},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        }


class OcrEngines:
    """Единственная точка, где provider секции [ocr] превращается в движок:
    роутер документов, индексатор и инструменты получают OcrEngine отсюда и
    о конкретных провайдерах не знают. Новый провайдер — новая секция в
    OcrConfig и ветка здесь."""

    def of(
        self, config: DisabledOcrConfig | RapidOcrConfig | OpenAiOcrConfig
    ) -> OcrEngine:
        match config:
            case RapidOcrConfig():
                return RapidOcrEngine(config)
            case OpenAiOcrConfig():
                return OpenAiOcrEngine(config)
            case DisabledOcrConfig():
                return DisabledOcr()

    def check(
        self, config: DisabledOcrConfig | RapidOcrConfig | OpenAiOcrConfig
    ) -> None:
        """Секция пригодна до старта работы: у rapidocr все файлы моделей на
        месте; endpoint openai проверяется первым же запросом."""
        match config:
            case RapidOcrConfig():
                RapidOcrEngine.check_models(config)
            case OpenAiOcrConfig():
                return
            case DisabledOcrConfig():
                return
