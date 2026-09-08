import type { ReactElement, SelectHTMLAttributes } from "react";

import "./Input.css";
import { controlClasses, type ControlMods } from "./Input";

type Props = SelectHTMLAttributes<HTMLSelectElement> & ControlMods;

/** Выпадающий выбор в общем облике полей ввода. */
export function Select({ mono, code, fill, narrow, className, children, ...rest }: Props): ReactElement {
  return (
    <select className={controlClasses({ mono, code, fill, narrow }, className)} {...rest}>
      {children}
    </select>
  );
}
