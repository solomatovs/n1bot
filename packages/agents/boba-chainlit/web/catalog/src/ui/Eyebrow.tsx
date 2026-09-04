import type { ReactElement, ReactNode } from "react";

import "./Eyebrow.css";

type Props = {
  children: ReactNode;
  /** Заголовок секции панели рендерится h4, метка блока — span. */
  as?: "span" | "h4";
};

/** Надзаголовок блока капителью. Единственное место с классом `eyebrow`. */
export function Eyebrow({ children, as = "span" }: Props): ReactElement {
  if (as === "h4") {
    return <h4 className="eyebrow">{children}</h4>;
  }

  return <span className="eyebrow">{children}</span>;
}
