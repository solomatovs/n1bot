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

import asyncio
import io
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

import numpy as np
import onnxruntime
from numpy.typing import NDArray
from PIL import Image
from rapidocr import EngineType, LangRec, ModelType, OCRVersion, RapidOCR
from rapidocr.utils.output import RapidOCROutput

from boba.doc.config import (
    ChatOcrConfig,
    DisabledOcrConfig,
    OcrModel,
    RapidOcrConfig,
)
from boba.doc.document import DisabledOcr, DocumentError, OcrEngine
from boba.llm.chat import (
    ChatImage,
    ChatModel,
    ChatRequest,
    ChatRole,
    ChatTurn,
    LlmError,
)
from boba.llm.providers import LlmProviders

__all__ = ["ChatOcrEngine", "OcrEngines", "OcrLines", "RapidOcrEngine"]


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

    def assemble(self, boxes: NDArray[Any], texts: Sequence[str]) -> str:
        words = sorted(self._words(boxes, texts), key=lambda word: word.center_y)
        lines: list[str] = []
        for line in self._lines(words):
            ordered = sorted(line, key=lambda word: word.left)
            lines.append(" ".join(word.text for word in ordered))

        return "\n".join(lines)

    def _words(self, boxes: NDArray[Any], texts: Sequence[str]) -> Iterator[OcrWord]:
        for box, text in zip(boxes, texts, strict=True):
            xs = box[:, 0]
            ys = box[:, 1]
            yield OcrWord(
                left=float(xs.min()),
                center_y=float((ys.min() + ys.max()) / 2),
                height=float(ys.max() - ys.min()),
                text=text,
            )

    def _lines(self, words: Sequence[OcrWord]) -> Iterator[list[OcrWord]]:
        line: list[OcrWord] = []
        for word in words:
            if not line:
                line.append(word)
                continue

            anchor = line[0]
            if abs(word.center_y - anchor.center_y) <= 0.5 * anchor.height:
                line.append(word)
                continue

            yield line
            line = [word]

        if line:
            yield line


class RapidOcrEngine(OcrEngine):
    """Реализация OcrEngine на rapidocr: модели по явным путям, вывод —
    строки текста в порядке чтения."""

    def __init__(self, config: RapidOcrConfig) -> None:
        self._config = config
        self._lines = OcrLines()
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

        return self._lines.assemble(output.boxes, output.txts)

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
            "Global.log_level": "warning",
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


class ChatOcrEngine(OcrEngine):
    """Реализация OcrEngine vision-чат-моделью проекта: картинка уходит PNG
    одним сообщением, обратно приходит текст. Ридеры зовут движок из своих
    потоков, поэтому ответ модели ждётся через loop приложения, на котором
    живёт транспорт модели."""

    IMAGE_FORMAT: ClassVar[str] = "PNG"
    IMAGE_MEDIA_TYPE: ClassVar[str] = "image/png"
    MAX_SIDE: ClassVar[int] = 2000

    def __init__(
        self,
        chat: ChatModel,
        config: ChatOcrConfig,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._chat = chat
        self._config = config
        self._loop = loop

    @property
    def enabled(self) -> bool:
        return True

    def recognize(self, image: Image.Image) -> str:
        request = self._request(self._fit(image))
        where = f"chat ocr with model {self._config.chat.model}"

        future = asyncio.run_coroutine_threadsafe(self._chat.reply(request), self._loop)
        try:
            reply = future.result()
        except LlmError as exc:
            raise DocumentError(f"{where}: {exc}") from exc

        text = reply.content.strip()
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

    def _request(self, image: Image.Image) -> ChatRequest:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format=self.IMAGE_FORMAT)

        turn = ChatTurn(
            role=ChatRole.USER,
            content=OcrPrompt.TRANSCRIBE.render(),
            images=[
                ChatImage(media_type=self.IMAGE_MEDIA_TYPE, data=buffer.getvalue())
            ],
        )

        return ChatRequest(
            messages=[turn],
            sampling=self._config.chat.sampling,
            stream=False,
        )


class OcrEngines:
    """Единственная точка, где provider секции [ocr] превращается в движок:
    роутер документов, индексатор и инструменты получают OcrEngine отсюда и
    о конкретных провайдерах не знают. Чат-модель для provider = chat берётся
    из реестра моделей процесса."""

    def __init__(self, llm: LlmProviders) -> None:
        self._llm = llm

    def of(
        self, config: DisabledOcrConfig | RapidOcrConfig | ChatOcrConfig
    ) -> OcrEngine:
        match config:
            case RapidOcrConfig():
                return RapidOcrEngine(config)
            case ChatOcrConfig():
                loop = asyncio.get_running_loop()
                return ChatOcrEngine(self._llm.chat(config.chat), config, loop)
            case DisabledOcrConfig():
                return DisabledOcr()

    def check(self, config: DisabledOcrConfig | RapidOcrConfig | ChatOcrConfig) -> None:
        """Секция пригодна до старта работы: у rapidocr все файлы моделей на
        месте, у chat — провайдер установлен и собирается."""
        match config:
            case RapidOcrConfig():
                RapidOcrEngine.check_models(config)
            case ChatOcrConfig():
                self._llm.chat(config.chat)
            case DisabledOcrConfig():
                return
