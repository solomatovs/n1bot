import { AlertTriangle, CheckCircle2, Info } from "lucide-react";
import type { PropsWithChildren, ReactElement } from "react";

import "./Alert.css";

export type Tone = "error" | "info" | "ok";

type Props = PropsWithChildren<{
  tone: Tone;
  title?: string;
  /** Метка для тестов: data-notice. */
  mark?: string;
}>;

const ICONS: Record<Tone, ReactElement> = {
  error: <AlertTriangle size={14} />,
  info: <Info size={14} />,
  ok: <CheckCircle2 size={14} />,
};

/** Единое сообщение страницы: блок с иконкой, заголовком и текстом любой длины. */
export function Alert({ tone, title, mark, children }: Props): ReactElement {
  return (
    <div className={`alert alert--${tone}`} role={tone === "error" ? "alert" : "status"} data-notice={mark}>
      <span className="alert__icon">{ICONS[tone]}</span>
      <div className="alert__body">
        {title !== undefined && <span className="alert__title">{title}</span>}
        <div className="alert__text">{children}</div>
      </div>
    </div>
  );
}
