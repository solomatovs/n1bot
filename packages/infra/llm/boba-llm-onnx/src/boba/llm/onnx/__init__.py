"""Провайдер onnx: локальная чат-модель на onnxruntime-genai."""

from __future__ import annotations

from boba.llm.onnx.chat import (
    MANIFEST,
    OnnxBackend,
    OnnxChatModel,
    OnnxChatRuntime,
    OnnxGenai,
    OnnxProvider,
)

__all__ = [
    "MANIFEST",
    "OnnxBackend",
    "OnnxChatModel",
    "OnnxChatRuntime",
    "OnnxGenai",
    "OnnxProvider",
]
