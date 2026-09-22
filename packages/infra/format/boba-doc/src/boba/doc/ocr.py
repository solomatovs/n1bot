"""OCR на моделях PP-OCR: детектор строк, классификатор ориентации и
распознаватель языка через rapidocr на onnxruntime. Живёт за extra `ocr`;
файлы моделей лежат в каталоге из конфига, из сети ничего не берётся.
Секция конфига — union по provider: off либо rapidocr, движок собирает OcrEngines.

Ошибки:
DocumentError — нет файлов моделей, движок не поднялся или распознавание
    сорвалось.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

import numpy as np
import onnxruntime
from numpy.typing import NDArray
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from rapidocr import EngineType, LangRec, ModelType, OCRVersion, RapidOCR
from rapidocr.utils.output import RapidOCROutput

from boba.doc.document import DisabledOcr, DocumentError, OcrEngine

__all__ = [
    "DisabledOcrConfig",
    "OcrConfig",
    "OcrEngines",
    "OcrLanguage",
    "OcrLines",
    "OcrModel",
    "RapidOcrConfig",
    "RapidOcrEngine",
]


class OcrLanguage(StrEnum):
    """Языки распознавателя PP-OCRv5; eslav — русский, украинский, белорусский."""

    ESLAV = "eslav"
    CYRILLIC = "cyrillic"
    LATIN = "latin"
    EN = "en"
    CH = "ch"
    KOREAN = "korean"
    ARABIC = "arabic"
    DEVANAGARI = "devanagari"
    TH = "th"
    EL = "el"
    TA = "ta"
    TE = "te"

    def lang_rec(self) -> LangRec:
        return LangRec(self.value)


class OcrModel(StrEnum):
    """Имена файлов моделей в каталоге моделей; распознаватель зависит от языка."""

    DET = "ch_PP-OCRv5_det_mobile.onnx"
    CLS = "ch_ppocr_mobile_v2.0_cls_mobile.onnx"
    REC = "{language}_PP-OCRv5_rec_mobile.onnx"

    def path(self, models_dir: Path, language: OcrLanguage) -> Path:
        return models_dir / self.value.format(language=language.value)

    @classmethod
    def required(cls, models_dir: Path, language: OcrLanguage) -> Iterator[Path]:
        for model in cls:
            yield model.path(models_dir, language)


class DisabledOcrConfig(BaseModel):
    """Секция OCR с provider = off: картинки и сканы дают пустой текст."""

    model_config = ConfigDict(frozen=True)

    provider: Literal["off"]

    @property
    def enabled(self) -> bool:
        return False

    def fingerprint(self) -> Mapping[str, object]:
        """Часть отпечатка индексатора: что влияет на распознанный текст."""
        return {"provider": self.provider}


class RapidOcrConfig(BaseModel):
    """Секция OCR с provider = rapidocr: каталог моделей, язык, порог
    уверенности и потоки onnxruntime."""

    model_config = ConfigDict(frozen=True)

    provider: Literal["rapidocr"]
    models_dir: Path
    language: OcrLanguage
    text_score: float = Field(ge=0.0, le=1.0)
    threads: int = Field(ge=1)

    @property
    def enabled(self) -> bool:
        return True

    def fingerprint(self) -> Mapping[str, object]:
        return {
            "provider": self.provider,
            "language": self.language.value,
            "text_score": self.text_score,
        }


OcrConfig = Annotated[
    DisabledOcrConfig | RapidOcrConfig,
    Field(discriminator="provider"),
]
"""Discriminated union по provider — точная диагностика ошибок валидации."""


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
        self._check_models()
        try:
            self._ocr = RapidOCR(params=self._params())
        except Exception as exc:
            providers = ", ".join(onnxruntime.get_available_providers())
            raise DocumentError(
                f"ocr: initializing rapidocr with models from {config.models_dir} "
                f"for language {config.language.value!r} on onnxruntime "
                f"{onnxruntime.__version__} ({providers}) failed: {exc}"
            ) from exc

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

    def _check_models(self) -> None:
        missing: list[str] = []
        for path in OcrModel.required(self._config.models_dir, self._config.language):
            if path.is_file():
                continue

            missing.append(path.name)

        if not missing:
            return

        raise DocumentError(
            f"ocr: models directory {self._config.models_dir} lacks "
            f"{', '.join(missing)} for language {self._config.language.value!r}"
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
            "Rec.lang_type": language.lang_rec(),
            "Rec.model_path": str(OcrModel.REC.path(models_dir, language)),
        }


class OcrEngines:
    """Фабрика движка по секции конфига: off — DisabledOcr, rapidocr — модели."""

    @staticmethod
    def of(config: DisabledOcrConfig | RapidOcrConfig) -> OcrEngine:
        if isinstance(config, RapidOcrConfig):
            return RapidOcrEngine(config)

        return DisabledOcr()
