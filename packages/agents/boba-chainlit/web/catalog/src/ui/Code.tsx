import type { ReactElement } from "react";

import "./Code.css";

/** Код в рамке с прокруткой: определение view, тело рутины.
 * Единственное место, где существует класс `code`. */
export function Code({ mark, children }: { mark?: string | undefined; children: string }): ReactElement {
  return (
    <pre className="code" data-testid={mark}>
      {children}
    </pre>
  );
}
