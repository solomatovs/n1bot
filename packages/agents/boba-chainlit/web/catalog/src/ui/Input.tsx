import type { InputHTMLAttributes, ReactElement } from "react";

import "./Input.css";

export type ControlMods = {
  /** Моноширинный ввод: значения аргументов, имена, идентификаторы. */
  mono?: boolean | undefined;
  /** Кодовое поле: без переносов настроек, моно и просторнее. */
  code?: boolean | undefined;
};

export function controlClasses({ mono = false, code = false }: ControlMods, extra?: string): string {
  const classes = ["input"];
  if (mono) {
    classes.push("mono");
  }
  if (code) {
    classes.push("input--code");
  }
  if (extra !== undefined) {
    classes.push(extra);
  }

  return classes.join(" ");
}

type Props = InputHTMLAttributes<HTMLInputElement> & ControlMods;

/** Однострочный ввод. Единственное место (вместе с TextArea/Select), где
 * существует класс `input`. */
export function Input({ mono, code, className, ...rest }: Props): ReactElement {
  return <input className={controlClasses({ mono, code }, className)} {...rest} />;
}
