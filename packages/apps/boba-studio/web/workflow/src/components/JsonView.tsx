import type { ReactElement } from "react";

import { jsonRows } from "../model/json";

type Props = {
  value: unknown;
  /** Обрезка строк до одной линии заданной длины; 0 — целиком. */
  clip: number;
};

/** Структурный показ JSON: ключи, скобки и значения разными цветами, вложенность отступом. */
export function JsonView({ value, clip }: Props): ReactElement {
  const rows = jsonRows(value, clip);

  return (
    <div className="json" data-clipped={clip > 0}>
      {rows.map((row, index) => (
        <div className="json__row" style={{ paddingLeft: `${row.depth * 12}px` }} key={index}>
          {row.key !== "" && <span className="json__key">{row.key}</span>}
          <span className={`json__${row.kind}`}>{row.text}</span>
        </div>
      ))}
    </div>
  );
}
