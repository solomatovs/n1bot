import type { InputHTMLAttributes, ReactElement } from "react";

import "./Input.css";

export type ControlMods = {
  /** Моноширинный ввод: значения аргументов, имена, идентификаторы. */
  mono?: boolean | undefined;
  /** Кодовое поле: без переносов настроек, моно и просторнее. */
  code?: boolean | undefined;
  /** Во всю ширину контейнера. */
  fill?: boolean | undefined;
  /** Короткое числовое поле в три высоты контрола. */
  narrow?: boolean | undefined;
};

export function controlClasses(
  { mono = false, code = false, fill = false, narrow = false }: ControlMods,
  extra?: string,
): string {
  const classes = ["input"];
  if (mono) {
    classes.push("mono");
  }
  if (code) {
    classes.push("input--code");
  }
  if (fill) {
    classes.push("input--fill");
  }
  if (narrow) {
    classes.push("input--narrow");
  }
  if (extra !== undefined) {
    classes.push(extra);
  }

  return classes.join(" ");
}

type Props = InputHTMLAttributes<HTMLInputElement> & ControlMods;

/** Однострочный ввод высотой в контрол. Единственное место (вместе с
 * TextArea/Select/Search), где существует класс `input`. */
export function Input({ mono, code, fill, narrow, className, ...rest }: Props): ReactElement {
  return <input className={controlClasses({ mono, code, fill, narrow }, className)} {...rest} />;
}
