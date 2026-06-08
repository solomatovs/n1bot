"""Построение cl.Plotly-элемента из доменного ChartResult.spec.

Единственное место, знающее о plotly: домен (ChartResult) оперирует чистым
dict'ом (Plotly figure JSON), а сборка go.Figure + cl.Plotly живёт здесь,
в presentation-слое. plotly импортируется лениво — пакет в deps агента.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

import chainlit as cl
from chainlit.element import ElementDisplay

__all__ = ["build_plotly_data_uri", "build_plotly_element"]


def build_plotly_element(
    name: str,
    spec: Mapping[str, Any],
    *,
    display: ElementDisplay = "inline",
) -> cl.Plotly:
    """ChartResult.spec (Plotly figure JSON) -> cl.Plotly.

    go.Figure(spec) валидирует структуру; невалидный spec бросит — вызывающий
    (live/replay) ловит и откатывается на текстовую заглушку.
    """
    from plotly import graph_objects as go  # noqa: PLC0415 — ленивый импорт plotly

    figure = go.Figure(spec)
    return cl.Plotly(name=name, figure=figure, display=display)


def build_plotly_data_uri(spec: Mapping[str, Any]) -> str:
    """ChartResult.spec -> data:-URI с figure-JSON (для восстановления из истории).

    Фронт chainlit грузит содержимое plotly-элемента по ElementDict.url. При
    reload файлового хранилища у нас нет — встраиваем figure-JSON прямо в url как
    data:-URI, источник — журнал (spec). Формат JSON тот же, что у live
    (pio.to_json(go.Figure(spec))), чтобы рендер совпадал.
    """
    from plotly import graph_objects as go  # noqa: PLC0415
    from plotly import io as pio  # noqa: PLC0415

    # to_json без файла всегда возвращает str; or "" — лишь чтобы снять
    # Optional из стаба.
    payload = pio.to_json(go.Figure(spec), validate=True) or ""
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return f"data:application/json;base64,{b64}"
